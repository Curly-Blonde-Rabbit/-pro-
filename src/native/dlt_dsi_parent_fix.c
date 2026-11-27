/* Select existing real DSI0 PLL parents. No fixed/dummy clock or rate override. */
#include <linux/module.h>
#include <linux/clk.h>
#include <linux/clk-provider.h>
static unsigned long lookup_fn_addr;
static bool apply;
module_param(lookup_fn_addr, ulong, 0400);
module_param(apply, bool, 0400);
typedef struct clk *(*lookup_fn)(const char *);
static int __init dlt_parent_fix_init(void)
{
    const char *children[] = {"disp_cc_mdss_byte0_clk_src","disp_cc_mdss_pclk0_clk_src"};
    const char *parents[] = {"dsi0_phy_pll_out_byteclk","dsi0_phy_pll_out_dsiclk"};
    int i,rc;
    if(!lookup_fn_addr)return -EINVAL;
    for(i=0;i<2;i++) {
        struct clk *c=((lookup_fn)lookup_fn_addr)(children[i]);
        struct clk *p=((lookup_fn)lookup_fn_addr)(parents[i]);
        struct clk *old;
        if(!c || !p)return -ENOENT;
        old=clk_get_parent(c);
        pr_info("DLT_DSI_PARENT_FIX %s before=%s expected=%s apply=%d\n",children[i],old?__clk_get_name(old):"NULL",parents[i],apply);
        if(apply && old!=p) {
            rc=clk_set_parent(c,p);
            pr_info("DLT_DSI_PARENT_FIX %s set_parent_rc=%d\n",children[i],rc);
            if(rc)return rc;
        }
        pr_info("DLT_DSI_PARENT_FIX %s after=%s round_137MHz=%ld\n",children[i],clk_get_parent(c)?__clk_get_name(clk_get_parent(c)):"NULL",clk_round_rate(c,137000000));
    }
    return 0;
}
static void __exit dlt_parent_fix_exit(void) { }
module_init(dlt_parent_fix_init);
module_exit(dlt_parent_fix_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Real DSI0 PLL parent selection diagnostic and repair");
