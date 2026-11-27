/* Per-boot, reversible NULL-parent guards; no synthetic clocks or rate votes. */
#include <linux/module.h>
#include <linux/clk.h>
#include <linux/clk-provider.h>
#include <linux/uaccess.h>
#include <linux/ratelimit.h>
#include <linux/slab.h>
#include <linux/rcupdate.h>
extern const struct clk_ops clk_byte2_ops;
extern const struct clk_ops clk_pixel_ops;
static unsigned long lock_fn_addr, unlock_fn_addr, lookup_fn_addr;
module_param(lock_fn_addr, ulong, 0400);
module_param(unlock_fn_addr, ulong, 0400);
module_param(lookup_fn_addr, ulong, 0400);
typedef int (*rate_fn)(struct clk_hw *, struct clk_rate_request *);
typedef void (*lock_fn)(void);
typedef struct clk *(*lookup_fn)(const char *);
static rate_fn original_byte, original_pixel;
static bool installed;
struct core_prefix { const char *name; const struct clk_ops *ops; };
struct patched_clock { struct core_prefix *core; const struct clk_ops *original; struct clk_ops *guarded; };
static struct patched_clock patched[4];
static const char *clock_names[4] = {
    "disp_cc_mdss_byte0_clk_src", "disp_cc_mdss_pclk0_clk_src",
    "disp_cc_mdss_byte1_clk_src", "disp_cc_mdss_pclk1_clk_src"
};
static int guard_byte(struct clk_hw *hw, struct clk_rate_request *req)
{
    if (!hw || !req || !req->best_parent_hw) {
        pr_warn_ratelimited("DLT_DSI_PARENT missing byte-clock parent; returning ENODEV\n");
        return -ENODEV;
    }
    return original_byte(hw, req);
}
static int guard_pixel(struct clk_hw *hw, struct clk_rate_request *req)
{
    if (!hw || !req || !req->best_parent_hw) {
        pr_warn_ratelimited("DLT_DSI_PARENT missing pixel-clock parent; returning ENODEV\n");
        return -ENODEV;
    }
    return original_pixel(hw, req);
}
static void dump_parent(const char *name)
{
    struct clk *clk = ((lookup_fn)lookup_fn_addr)(name);
    struct clk_hw *hw, *p;
    int i,n;
    if (!clk) { pr_info("DLT_DSI_PARENT %s absent\n",name);return; }
    hw=__clk_get_hw(clk);p=clk_hw_get_parent(hw);n=clk_hw_get_num_parents(hw);
    pr_info("DLT_DSI_PARENT %s current=%s choices=%d\n",name,p?clk_hw_get_name(p):"NULL",n);
    for(i=0;i<n && i<8;i++) {
        p=clk_hw_get_parent_by_index(hw,i);
        pr_info("DLT_DSI_PARENT %s index=%d parent=%s\n",name,i,p?clk_hw_get_name(p):"NULL");
    }
}
static int __init dlt_guard_init(void)
{
    int i,rc=0;
    if (!lock_fn_addr || !unlock_fn_addr || !lookup_fn_addr)return -EINVAL;
    ((lock_fn)lock_fn_addr)();
    original_byte=clk_byte2_ops.determine_rate;
    original_pixel=clk_pixel_ops.determine_rate;
    if (!original_byte || !original_pixel) { ((lock_fn)unlock_fn_addr)();return -EINVAL; }
    for(i=0;i<4;i++) {
        struct clk *clk=((lookup_fn)lookup_fn_addr)(clock_names[i]);
        struct clk_hw *hw;
        if(!clk) { rc=-ENOENT;break; }
        hw=__clk_get_hw(clk);patched[i].core=(void *)hw->core;
        patched[i].original=patched[i].core->ops;
        if(patched[i].original!=&clk_byte2_ops && patched[i].original!=&clk_pixel_ops) { rc=-EINVAL;break; }
        patched[i].guarded=kmemdup(patched[i].original,sizeof(struct clk_ops),GFP_KERNEL);
        if(!patched[i].guarded) { rc=-ENOMEM;break; }
        patched[i].guarded->determine_rate=(patched[i].original==&clk_byte2_ops)?guard_byte:guard_pixel;
        if(probe_kernel_write(&patched[i].core->ops,&patched[i].guarded,sizeof(patched[i].guarded))) {rc=-EFAULT;break;}
    }
    if(rc) {
        int j;
        for(j=0;j<4;j++)if(patched[j].guarded) {
            probe_kernel_write(&patched[j].core->ops,&patched[j].original,sizeof(patched[j].original));
            kfree(patched[j].guarded);
        }
        ((lock_fn)unlock_fn_addr)();return rc;
    }
    installed=true;
    pr_info("DLT_DSI_PARENT guards installed byte=%ps pixel=%ps\n",original_byte,original_pixel);
    dump_parent("disp_cc_mdss_byte0_clk_src");
    dump_parent("disp_cc_mdss_pclk0_clk_src");
    dump_parent("dsi0_phy_pll_out_byteclk");
    dump_parent("dsi0_phy_pll_out_dsiclk");
    pr_info("DLT_DSI_PARENT null-input selftest byte=%d pixel=%d\n",guard_byte(NULL,NULL),guard_pixel(NULL,NULL));
    ((lock_fn)unlock_fn_addr)();
    return 0;
}
static void __exit dlt_guard_exit(void)
{
    int i;
    if (!installed)return;
    ((lock_fn)lock_fn_addr)();
    for(i=0;i<4;i++)
        probe_kernel_write(&patched[i].core->ops,&patched[i].original,sizeof(patched[i].original));
    ((lock_fn)unlock_fn_addr)();
    synchronize_sched();
    for(i=0;i<4;i++)kfree(patched[i].guarded);
    pr_info("DLT_DSI_PARENT original callbacks restored\n");
}
module_init(dlt_guard_init);
module_exit(dlt_guard_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Reversible DSI NULL-parent rate guards and parent diagnostics");
