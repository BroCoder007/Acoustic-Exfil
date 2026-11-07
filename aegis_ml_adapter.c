#include "aegis_ml_adapter.h"
void aegis_ml_adapter_init(aegis_ml_adapter *a)
{
    aegis_ml_init(&a->model);
    a->used = 0; a->dc = 2048.0f;
}
uint32_t aegis_ml_adapter_push(aegis_ml_adapter *a, uint16_t adc, uint16_t output[128])
{
    float y = (float)adc - a->dc;
    a->dc += y * (1.0f / 256.0f);
    a->input[a->used++] = y / 2048.0f;
    if (a->used < 128u) return 0;
    a->used = 0;
    aegis_ml_process(&a->model, a->input, a->output);
    for (uint32_t i = 0; i < 128u; i++) {
        float v = 2048.0f + 2048.0f * a->output[i];
        if (v < 0.0f) v = 0.0f;
        if (v > 4095.0f) v = 4095.0f;
        output[i] = (uint16_t)(v + .5f);
    }
    return 128u;
}
