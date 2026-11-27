/* Independent generated IRIS lifecycle sequencer; not a loaded driver. */
#ifndef DLT_IRIS_ENGINE_H
#define DLT_IRIS_ENGINE_H
struct dlt_iris_ops {
    /* Native Qualcomm host->transfer returns 0 on successful TX. */
    int (*tx)(void *, unsigned int type, unsigned int flags,
              const unsigned char *data, unsigned int length);
    int (*read)(void *, unsigned int address, unsigned int *value);
    int (*sleep_us)(void *, unsigned int usec);
    unsigned long long (*now_us)(void *);
    int (*cancelled)(void *);
};
struct dlt_iris_engine {
    struct dlt_iris_ops ops;
    void *opaque;
    unsigned int state; /* 0=cold, 1=prepared, 2=lit, 3=failed */
    unsigned int completed_commands;
    unsigned int last_step;
    int last_error;
};
/* Caller holds panel/lifecycle lock and provides bounded transport callbacks.
 * Caller must power-cycle IRIS + panel after any failure before initializing
 * a fresh engine. These APIs neither reset the device nor touch clocks/GPIOs.
 * Pre goes BEFORE panel ON command set; post goes AFTER panel ON command set.
 * The panel commands themselves must be forwarded through the IRIS FIFO.
 */
int dlt_iris_pre_lightup(struct dlt_iris_engine *engine);
int dlt_iris_post_lightup(struct dlt_iris_engine *engine);
#endif
