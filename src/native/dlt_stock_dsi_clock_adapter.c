/* Bridge the stock darklighter clock binding to the native staging display. */
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/clk.h>
#include <linux/clk-provider.h>
#include <linux/of.h>
#include "dsi_display.h"
static bool apply;
module_param(apply,bool,0400);
static struct device *dev;
static struct dsi_display *display;
static struct clk *handles[4];
static struct dsi_clk_link_set old_src,old_mux;
static bool installed,old_dynamic;
static int __init adapter_init(void)
{
    const char *names[4]={"src_byte_clk0","src_pixel_clk0","src_byte_clk0","src_pixel_clk0"};
    int i,rc=0;
    dev=bus_find_device_by_name(&platform_bus_type,NULL,"soc:qcom,dsi-display-primary");
    if(!dev)return -ENODEV;
    if(!dev->driver || strcmp(dev->driver->name,"msm-dsi-display")) {rc=-ENODEV;goto fail;}
    display=dev_get_drvdata(dev);
    if(!display || display->pdev!=to_platform_device(dev) || !display->panel) {rc=-EINVAL;goto fail;}
    if(of_find_property(dev->of_node,"qcom,dsi-select-clocks",NULL) ||
       of_property_match_string(dev->of_node,"clock-names","src_byte_clk0")<0 ||
       of_property_match_string(dev->of_node,"clock-names","src_pixel_clk0")<0) {rc=-EINVAL;goto fail;}
    mutex_lock(&display->display_lock);
    pr_info("DLT_DSI_BINDING display=%s src_byte=%px src_pixel=%px mux_byte=%px mux_pixel=%px apply=%d\n",
        display->name,display->clock_info.src_clks.byte_clk,display->clock_info.src_clks.pixel_clk,
        display->clock_info.mux_clks.byte_clk,display->clock_info.mux_clks.pixel_clk,apply);
    pr_info("DLT_DSI_BINDING existing source byte=%s pixel=%s\n",
        display->clock_info.src_clks.byte_clk?__clk_get_name(display->clock_info.src_clks.byte_clk):"NULL",
        display->clock_info.src_clks.pixel_clk?__clk_get_name(display->clock_info.src_clks.pixel_clk):"NULL");
    if(!apply) {mutex_unlock(&display->display_lock);return 0;}
    if(display->clock_info.mux_clks.byte_clk || display->clock_info.mux_clks.pixel_clk) {
        rc=-EEXIST;goto unlock;
    }
    for(i=0;i<4;i++) {
        handles[i]=devm_clk_get(dev,names[i]);
        if(IS_ERR_OR_NULL(handles[i])) {rc=handles[i]?PTR_ERR(handles[i]):-ENOENT;handles[i]=NULL;goto put_clocks;}
        pr_info("DLT_DSI_BINDING handle%d=%s via %s\n",i,__clk_get_name(handles[i]),names[i]);
    }
    old_src=display->clock_info.src_clks;old_mux=display->clock_info.mux_clks;
    if(!display->clock_info.src_clks.byte_clk)display->clock_info.src_clks.byte_clk=handles[0];
    if(!display->clock_info.src_clks.pixel_clk)display->clock_info.src_clks.pixel_clk=handles[1];
    display->clock_info.mux_clks.byte_clk=handles[2];
    display->clock_info.mux_clks.pixel_clk=handles[3];
    old_dynamic=display->panel->dyn_clk_caps.dyn_clk_support;
    display->panel->dyn_clk_caps.dyn_clk_support=false;
    installed=true;
    pr_info("DLT_DSI_BINDING stock DSI0 handles installed; dynamic refresh disabled\n");
    mutex_unlock(&display->display_lock);return 0;
put_clocks:
    for(i=0;i<4;i++)if(handles[i])devm_clk_put(dev,handles[i]);
unlock:
    mutex_unlock(&display->display_lock);
fail:
    put_device(dev);dev=NULL;return rc;
}
static void __exit adapter_exit(void)
{
    int i;
    if(!dev)return;
    if(installed && dev_get_drvdata(dev)==display) {
        mutex_lock(&display->display_lock);
        /* Unload only after the display trial has stopped. */
        display->clock_info.src_clks=old_src;display->clock_info.mux_clks=old_mux;
        display->panel->dyn_clk_caps.dyn_clk_support=old_dynamic;
        mutex_unlock(&display->display_lock);
        for(i=0;i<4;i++)devm_clk_put(dev,handles[i]);
    }
    put_device(dev);
    pr_info("DLT_DSI_BINDING stock adapter removed\n");
}
module_init(adapter_init);module_exit(adapter_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Stock darklighter DSI clock binding adapter");
