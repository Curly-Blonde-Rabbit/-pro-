#include "dlt_iris_engine.h"
#include "dlt_iris_stock_data.inc"
#define EINVAL 22
#define EIO 5
#define ETIMEDOUT 110
#define ECANCELED 125
#define EALREADY 114

static int fail(struct dlt_iris_engine *e, int rc)
{ e->state=3; e->last_error=rc; return rc; }
static int valid(struct dlt_iris_engine *e)
{ return e && e->ops.tx && e->ops.read && e->ops.sleep_us && e->ops.now_us; }
static int interrupted(struct dlt_iris_engine *e, unsigned long long end)
{
    if(e->ops.cancelled && e->ops.cancelled(e->opaque))return -ECANCELED;
    if(e->ops.now_us(e->opaque)>=end)return -ETIMEDOUT;
    return 0;
}
static int poll(struct dlt_iris_engine *e, const struct step *s,
                unsigned long long end)
{
    unsigned int n,v;
    int rc;
    for(n=0;n<50;n++) {
        rc=interrupted(e,end);if(rc)return rc;
        rc=e->ops.read(e->opaque,s->offset,&v);if(rc)return rc<0?rc:-EIO;
        if(s->kind==4) {
            if(v&0x180U)return -EIO;
            if(v&0x400U)return 0;
        } else if((v&0xffU)==s->value)return 0;
        rc=e->ops.sleep_us(e->opaque,s->kind==4?1000U:500U);
        if(rc)return rc<0?rc:-EIO;
    }
    return -ETIMEDOUT;
}
static int run(struct dlt_iris_engine *e,unsigned int begin,unsigned int end,
               unsigned int budget_us)
{
    unsigned long long deadline=e->ops.now_us(e->opaque)+budget_us;
    unsigned int i;
    int rc;
    for(i=begin;i<end;i++) {
        const struct step *s=&steps[i];
        e->last_step=i;
        rc=interrupted(e,deadline);if(rc)return fail(e,rc);
        if(s->kind==1) {
            if(s->offset>sizeof(stock_payload) ||
               s->length>sizeof(stock_payload)-s->offset)return fail(e,-EINVAL);
            rc=e->ops.tx(e->opaque,s->type,s->flags,
                        stock_payload+s->offset,s->length);
            /* Native host returns zero, a byte-count adapter may return len. */
            if(rc && rc!=(int)s->length)return fail(e,rc<0?rc:-EIO);
            e->completed_commands++;
        } else if(s->kind==2) {
            rc=e->ops.sleep_us(e->opaque,s->value);
            if(rc)return fail(e,rc<0?rc:-EIO);
        } else {
            rc=poll(e,s,deadline);if(rc)return fail(e,rc);
        }
    }
    rc=interrupted(e,deadline);return rc?fail(e,rc):0;
}
int dlt_iris_pre_lightup(struct dlt_iris_engine *e)
{
    unsigned int chip=0;
    int rc;
    if(!valid(e))return -EINVAL;
    if(e->state)return -EALREADY;
    rc=e->ops.read(e->opaque,0xf0000230U,&chip);
    if(rc)return fail(e,rc<0?rc:-EIO);
    if(chip!=0x4727a000U)return fail(e,-EINVAL);
    rc=run(e,0,21U,1000000U);
    if(!rc)e->state=1;
    return rc;
}
int dlt_iris_post_lightup(struct dlt_iris_engine *e)
{
    int rc;
    if(!valid(e))return -EINVAL;
    if(e->state!=1)return -EINVAL;
    rc=run(e,21U,sizeof(steps)/sizeof(steps[0]),10000000U);
    if(!rc)e->state=2;
    return rc;
}
