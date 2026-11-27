obj-m += dlt_iris_live.o
dlt_iris_live-y := dlt_iris_live_bridge.o dlt_iris_engine.o
obj-m += dlt_stock_dsi_clock_adapter.o dlt_dsi_parent_fix.o dlt_dsi_parent_guard.o
obj-m += dlt_dsx.o
dlt_dsx-y := touch/dlt_synaptics_dsx_core.o touch/dlt_synaptics_dsx_i2c.o
ccflags-y += -I$(srctree)/drivers/gpu/drm/msm/dsi-staging -I$(srctree)/drivers/gpu/drm/msm -I$(srctree)/drivers/gpu/drm/msm/sde -I$(srctree)/include/drm -Wno-unused-function -Wno-unused-label
