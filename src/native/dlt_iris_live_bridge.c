/* RAM-only, explicitly armed darklighter IRIS adaptation trial.
 * No boot service, flash, network, power-off or kernel restart interfaces.
 */
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/i2c.h>
#include <linux/of_gpio.h>
#include <linux/gpio.h>
#include <linux/gpio/consumer.h>
#include <linux/clk.h>
#include <linux/clk-provider.h>
#include <linux/delay.h>
#include <linux/ktime.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>
#include <asm/unaligned.h>
#include "dsi_display.h"
#include "dlt_iris_engine.h"

static bool allow_test;
module_param(allow_test,bool,0400);
/* On the current boot 1v1 is already enabled. Do not reconfigure a power
 * pin reserved by the generic, unused PCIe node. Require it to stay high.
 */
static bool preserve_existing_power=true;
module_param(preserve_existing_power,bool,0400);
static struct device *display_dev,*i2c_dev;
static struct dsi_display *display;
static struct i2c_client *client;
static struct clk *refclk;
static struct kobject *control_kobj;
static struct dlt_iris_engine engine;
static DEFINE_MUTEX(trial_lock);
static int reset_gpio=-1,power_gpio=-1,old_reset_dir,old_power_dir;
static int old_reset,old_power,last_rc,last_power=-1;
static int init_rc,last_status_rc;
static u32 last_rom,last_status_irq,last_status_word;
static bool gpios_owned,power_owned,clock_on,esd_saved,old_esd,initialized;
static unsigned int transactions;

static int reg_read(unsigned int address,unsigned int *value)
{
    u8 tx[5]={0xcc},rx[4];
    struct i2c_msg msg[2];
    int rc;
    put_unaligned_le32(address,tx+1);
    msg[0]=(struct i2c_msg){.addr=client->addr,.len=5,.buf=tx};
    msg[1]=(struct i2c_msg){.addr=client->addr,.flags=I2C_M_RD,.len=4,.buf=rx};
    rc=i2c_transfer(client->adapter,msg,2);transactions++;
    if(rc!=2)return rc<0?rc:-EIO;
    *value=get_unaligned_be32(rx);return 0;
}
static int reg_write(unsigned int address,unsigned int value)
{
    u8 data[9]={0xcc};int rc;
    struct i2c_msg msg={.addr=client->addr,.len=9,.buf=data};
    put_unaligned_le32(address,data+1);put_unaligned_be32(value,data+5);
    rc=i2c_transfer(client->adapter,&msg,1);transactions++;
    return rc==1?0:(rc<0?rc:-EIO);
}
static int cb_read(void *unused,unsigned int address,unsigned int *value)
{return reg_read(address,value);}
static int cb_sleep(void *unused,unsigned int usec)
{if(usec>200000)return -EINVAL;usleep_range(usec,usec+10);return 0;}
static unsigned long long cb_now(void *unused)
{return ktime_to_us(ktime_get());}
static int cb_tx(void *unused,unsigned int type,unsigned int flags,
                 const unsigned char *data,unsigned int length)
{
    struct mipi_dsi_msg msg={.channel=0,.type=type,.flags=flags,
                            .tx_buf=data,.tx_len=length};
    return display->host.ops->transfer(&display->host,&msg);
}

/* Exact stock iris_mipitx_cmd_send framing, without unrestricted register API. */
static int panel_tx(const struct mipi_dsi_msg *msg)
{
    const u8 *p=msg->tx_buf;u32 header,value;unsigned int i,j;
    u32 mode=(msg->flags&MIPI_DSI_MSG_USE_LPM)?3U:1U;
    int rc;
    if(!p || !msg->tx_len || msg->tx_len>1024 || msg->channel)return -EINVAL;
    switch(msg->type) {
    case 0x05:case 0x15:case 0x03:case 0x13:case 0x23:
        if(msg->tx_len>2)return -EINVAL;
        header=msg->type|((u32)p[0]<<8)|(mode<<24);
        if(msg->tx_len==2)header|=(u32)p[1]<<16;
        return reg_write(0xf019c010U,header);
    case 0x29:case 0x39:
        header=msg->type|((u32)msg->tx_len<<8)|(mode<<24)|0x04000000U;
        rc=reg_write(0xf019c010U,header);if(rc)return rc;
        for(i=0;i<msg->tx_len;i+=4) {
            value=0;
            for(j=0;j<4 && i+j<msg->tx_len;j++)value|=(u32)p[i+j]<<(j*8);
            rc=reg_write(0xf019c014U,value);if(rc)return rc;
        }
        return 0;
    default:return -EOPNOTSUPP;
    }
}
static int panel_set(enum dsi_cmd_set_type type)
{
    struct dsi_panel_cmd_set *set;
    struct dsi_cmd_desc *cmd;
    unsigned int i;int rc;
    if(!display->panel->cur_mode || !display->panel->cur_mode->priv_info)return -EINVAL;
    set=&display->panel->cur_mode->priv_info->cmd_sets[type];
    if(!set->count || set->count>128 || !set->cmds)return -EINVAL;
    for(i=0;i<set->count;i++) {
        struct mipi_dsi_msg msg;
        cmd=&set->cmds[i];msg=cmd->msg;
        if(set->state==DSI_CMD_SET_STATE_LP)msg.flags|=MIPI_DSI_MSG_USE_LPM;
        if(type==DSI_CMD_SET_ON) {
            /* Stock dsi_panel_tx_cmd_set (image +0x794060) uses host DSI
             * after iris_pre_lightup selects panel pass-through mode. */
            if(cmd->last_command)msg.flags|=MIPI_DSI_MSG_LASTCOMMAND;
            rc=display->host.ops->transfer(&display->host,&msg);
            if(rc>0 && rc!=(int)msg.tx_len)rc=-EIO;
        } else rc=panel_tx(&msg);
        if(rc<0)return rc;
        if(cmd->post_wait_ms>1000)return -EINVAL;
        if(cmd->post_wait_ms)msleep(cmd->post_wait_ms);
    }
    return 0;
}
static int panel_status(void)
{
    unsigned int n,v=0;int rc;
    /* Stock iris_mipitx_read_fifo_empty_check, bounded to ten drain words. */
    for(n=0;n<10;n++) {
        rc=reg_read(0xf0180070U,&v);if(rc)return rc;
        if(v&0x8000U)break;
        rc=reg_read(0xf019c018U,&v);if(rc)return rc;
    }
    rc=reg_write(0xf019fff0U,0x80000000U);if(rc)return rc;
    rc=reg_write(0xf019c010U,0x01000137U);if(rc)return rc;
    /* Stock iris2p_read_panel_reg uses 6 | (register << 8), without mode bits. */
    rc=reg_write(0xf019c010U,0x00000a06U);if(rc)return rc;
    for(n=0;n<50;n++) {
        rc=reg_read(0xf019ffe4U,&v);if(rc)return rc;
        last_status_irq=v;
        if(v&0x80000000U) {
            rc=reg_read(0xf019c018U,&v);if(rc)return rc;
            last_status_word=v;
            if((v&255U)!=0x21U && (v&255U)!=0x22U)return -EPROTO;
            last_power=(v>>8)&255U;return 0;
        }
        usleep_range(2000,2010);
    }
    return -ETIMEDOUT;
}
static void release_pins(void)
{
    if(gpios_owned) {
        if(old_reset_dir==0)gpio_direction_output(reset_gpio,old_reset);
        else gpio_direction_input(reset_gpio);
        if(power_owned) {
            if(old_power_dir==0)gpio_direction_output(power_gpio,old_power);
            else gpio_direction_input(power_gpio);
            gpio_free(power_gpio);power_owned=false;
        }
        gpio_free(reset_gpio);gpios_owned=false;
    }
}
static int reset_bridge(void)
{
    int rc;
    old_reset_dir=gpiod_get_direction(gpio_to_desc(reset_gpio));
    old_power_dir=gpiod_get_direction(gpio_to_desc(power_gpio));
    if(old_reset_dir<0 || old_power_dir<0)return -EIO;
    old_reset=gpio_get_value(reset_gpio);old_power=gpio_get_value(power_gpio);
    if(preserve_existing_power) {
        if(old_power!=1)return -EHOSTDOWN;
    } else {
        rc=gpio_request(power_gpio,"dlt-iris-1v1-trial");if(rc)return rc;
        power_owned=true;
    }
    rc=gpio_request(reset_gpio,"dlt-iris-reset-trial");
    if(rc){if(power_owned)gpio_free(power_gpio);power_owned=false;return rc;}
    gpios_owned=true;
    if(!clock_on)return -EHOSTDOWN;
    if(power_owned){rc=gpio_direction_output(power_gpio,1);if(rc)return rc;}
    rc=gpio_direction_output(reset_gpio,1);if(rc)return rc;
    usleep_range(5000,5010);gpio_set_value(reset_gpio,0);
    usleep_range(6000,6010);gpio_set_value(reset_gpio,1);
    usleep_range(10000,10010);return 0;
}
static int reset_panel_after_bridge(void)
{
    struct dsi_panel *panel=display->panel;
    const u32 expected[6]={1,10,0,10,1,0};u32 sequence[6];
    unsigned int i;int rc;
    if(panel->reset_config.reset_gpio!=6)return -EINVAL;
    rc=of_property_read_u32_array(panel->panel_of_node,
              "qcom,mdss-dsi-reset-sequence",sequence,6);
    if(rc || memcmp(sequence,expected,sizeof(sequence)))return -EINVAL;
    /* GPIO6 is already owned by this same native panel, whose lock is held.
     * Stock powers/resets IRIS first, then resets the physical OLED. */
    rc=gpio_direction_output(panel->reset_config.reset_gpio,sequence[0]);
    if(rc)return rc;
    for(i=0;i<6;i+=2) {
        gpio_set_value(panel->reset_config.reset_gpio,sequence[i]);
        if(sequence[i+1])msleep(sequence[i+1]);
    }
    return 0;
}
static int initialize(void)
{
    int rc;unsigned int chip=0,rom=0;
    if(initialized || gpios_owned)return -EALREADY;
    if(!display->panel->panel_initialized)return -EHOSTDOWN;
    if(!display->clock_info.mux_clks.byte_clk ||
       !display->clock_info.mux_clks.pixel_clk)return -ENXIO;
    rc=reset_bridge();if(rc)goto fail;
    rc=reg_read(0xf0000230U,&chip);if(rc)goto fail;
    if(chip!=0x4727a000U){rc=-ENODEV;goto fail;}
    rc=reset_panel_after_bridge();if(rc)goto fail;
    memset(&engine,0,sizeof(engine));
    engine.ops.tx=cb_tx;engine.ops.read=cb_read;
    engine.ops.sleep_us=cb_sleep;engine.ops.now_us=cb_now;
    rc=dlt_iris_pre_lightup(&engine);if(rc)goto fail;
    rc=panel_set(DSI_CMD_SET_ON);if(rc)goto fail;
    rc=dlt_iris_post_lightup(&engine);if(rc)goto fail;
    initialized=true;msleep(30);
    rc=reg_read(0xf0100000U,&rom);
    last_rom=rom;
    pr_info("DLT_IRIS initialized rom=%x commands=%u rc=%d\n",rom,engine.completed_commands,rc);
    if(!rc)rc=panel_status();
    last_status_rc=rc;
    return rc;
fail:
    pr_err("DLT_IRIS initialization failed rc=%d state=%u step=%u commands=%u\n",
            rc,engine.state,engine.last_step,engine.completed_commands);
    release_pins();return rc;
}
static ssize_t status_show(struct kobject *kobj,struct kobj_attribute *attr,char *buf)
{
    return scnprintf(buf,PAGE_SIZE,"armed=%d initialized=%d rc=%d init_rc=%d state=%u step=%u commands=%u i2c_transactions=%u panel_power=%d panel_status_rc=%d irq_status=%x response=%x rom=%x reset_gpio=%d power_gpio=%d refclk=%s rate=%lu\n",
            allow_test,initialized,last_rc,init_rc,engine.state,engine.last_step,
            engine.completed_commands,transactions,last_power,last_status_rc,
            last_status_irq,last_status_word,last_rom,reset_gpio,power_gpio,
            __clk_get_name(refclk),clk_get_rate(refclk));
}
static ssize_t control_store(struct kobject *kobj,struct kobj_attribute *attr,
                             const char *buf,size_t count)
{
    int rc=0;
    if(!allow_test)return -EPERM;
    if(!mutex_trylock(&trial_lock))return -EBUSY;
    mutex_lock(&display->display_lock);mutex_lock(&display->panel->panel_lock);
    if(sysfs_streq(buf,"initialize")){rc=initialize();init_rc=rc;}
    else if(sysfs_streq(buf,"status")){rc=panel_status();last_status_rc=rc;}
    else if(sysfs_streq(buf,"brightness64")) {
        const u8 level[3]={0x51,0x00,0x40};
        struct mipi_dsi_msg msg={.channel=0,.type=0x39,.flags=MIPI_DSI_MSG_USE_LPM,
                                .tx_buf=level,.tx_len=sizeof(level)};
        rc=initialized?panel_tx(&msg):-EHOSTDOWN;
    }
    else if(sysfs_streq(buf,"stop")) {
        if(initialized)rc=panel_set(DSI_CMD_SET_OFF);
        initialized=false;release_pins();
    } else rc=-EINVAL;
    last_rc=rc;
    mutex_unlock(&display->panel->panel_lock);mutex_unlock(&display->display_lock);
    mutex_unlock(&trial_lock);
    return rc?rc:count;
}
static struct kobj_attribute status_attr=__ATTR_RO(status);
static struct kobj_attribute control_attr=__ATTR(control,0200,NULL,control_store);
static int __init bridge_init(void)
{
    struct device_node *node;int rc;
    display_dev=bus_find_device_by_name(&platform_bus_type,NULL,"soc:qcom,dsi-display-primary");
    if(!display_dev)return -ENODEV;
    display=dev_get_drvdata(display_dev);
    if(!display_dev->driver || strcmp(display_dev->driver->name,"msm-dsi-display") ||
       !display || display->pdev!=to_platform_device(display_dev) || !display->panel ||
       strcmp(display->panel->name,"ss_amoled_fhd")){rc=-EINVAL;goto put_display;}
    node=display->panel->panel_of_node;
    reset_gpio=of_get_named_gpio(node,"qcom,dsp-reset-gpio",0);
    power_gpio=of_get_named_gpio(node,"qcom,dsp-dsp_1v1",0);
    if(reset_gpio!=9 || power_gpio!=37){rc=-EINVAL;goto put_display;}
    i2c_dev=bus_find_device_by_name(&i2c_bus_type,NULL,"2-0022");
    if(!i2c_dev){rc=-ENODEV;goto put_display;}
    client=to_i2c_client(i2c_dev);
    if(client->addr!=0x22 || i2c_dev->driver ||
       !of_device_is_compatible(i2c_dev->of_node,"pxlw,iris2p_i2c")){rc=-EBUSY;goto put_i2c;}
    refclk=clk_get(display_dev,"div_clk3");
    if(IS_ERR(refclk)){rc=PTR_ERR(refclk);goto put_i2c;}
    /* IRIS I2C also needs the reference clock. Hold exactly one clock vote
     * for the module lifetime, including probe and between bounded trials. */
    rc=clk_prepare_enable(refclk);if(rc)goto put_clk;
    clock_on=true;usleep_range(10000,10010);
    /* Native DRM powers down shared panel rails on detach. Do not expect
     * I2C to answer during module probe; validate chip after powered reset. */
    control_kobj=kobject_create_and_add("dlt_iris_trial",kernel_kobj);
    if(!control_kobj){rc=-ENOMEM;goto put_clk;}
    rc=sysfs_create_file(control_kobj,&status_attr.attr);if(rc)goto put_kobj;
    rc=sysfs_create_file(control_kobj,&control_attr.attr);if(rc)goto put_kobj;
    if(allow_test) {
        mutex_lock(&display->panel->panel_lock);
        if(display->panel->panel_initialized || atomic_read(&display->panel->esd_recovery_pending)) {
            mutex_unlock(&display->panel->panel_lock);rc=-EBUSY;goto put_kobj;
        }
        old_esd=display->panel->esd_config.esd_enabled;
        display->panel->esd_config.esd_enabled=false;esd_saved=true;
        mutex_unlock(&display->panel->panel_lock);
    }
    pr_info("DLT_IRIS trial armed=%d clk=%s rate=%lu; chip validation deferred until panel rails and reset active\n",
            allow_test,__clk_get_name(refclk),clk_get_rate(refclk));
    return 0;
put_kobj:kobject_put(control_kobj);control_kobj=NULL;
put_clk:if(clock_on){clk_disable_unprepare(refclk);clock_on=false;}clk_put(refclk);
put_i2c:put_device(i2c_dev);i2c_dev=NULL;
put_display:put_device(display_dev);display_dev=NULL;return rc;
}
static void __exit bridge_exit(void)
{
    if(control_kobj)kobject_put(control_kobj);
    mutex_lock(&trial_lock);
    release_pins();
    if(esd_saved) {
        mutex_lock(&display->panel->panel_lock);
        display->panel->esd_config.esd_enabled=old_esd;
        mutex_unlock(&display->panel->panel_lock);
    }
    mutex_unlock(&trial_lock);
    if(clock_on){clk_disable_unprepare(refclk);clock_on=false;}
    clk_put(refclk);put_device(i2c_dev);put_device(display_dev);
    pr_info("DLT_IRIS trial removed; pin directions and ESD setting restored\n");
}
module_init(bridge_init);module_exit(bridge_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Opt-in RAM-only stock IRIS bridge lifecycle trial");
