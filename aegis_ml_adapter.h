#ifndef AEGIS_ML_ADAPTER_H
#define AEGIS_ML_ADAPTER_H
#include "aegis_ml_denoise.h"
typedef struct {
    aegis_ml_state model;
    float input[128], output[128], dc;
    uint32_t used;
} aegis_ml_adapter;
void aegis_ml_adapter_init(aegis_ml_adapter *a);
/* Foreground only. Returns 128 when output[] contains a complete filtered block. */
uint32_t aegis_ml_adapter_push(aegis_ml_adapter *a, uint16_t adc, uint16_t output[128]);
#endif
