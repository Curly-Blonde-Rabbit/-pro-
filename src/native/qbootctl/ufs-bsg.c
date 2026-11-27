#include <stdint.h>
#include <errno.h>
int ufs_bsg_dev_open(void) { return -EOPNOTSUPP; }
int32_t set_boot_lun(uint8_t id) { (void)id; return -EOPNOTSUPP; }
