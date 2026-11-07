#ifndef AEGIS_ML_DENOISE_H
#define AEGIS_ML_DENOISE_H
#include <stdint.h>

/* Research front end, enabled by the project's main.c.
 * Audible 2/3 kHz only, 40 kHz mono, signed/DC-removed float samples.
 * Process 128 samples at a time. Output has 128 samples of algorithmic delay.
 * Never call this full block routine from the 40 kHz ADC interrupt.
 */
#define AEGIS_ML_N 256u
#define AEGIS_ML_HOP 128u
#define AEGIS_ML_BINS 32u
#define AEGIS_ML_HIDDEN 24u
typedef struct {
    float history[256], overlap[128], re[256], im[256];
    float previous[32], feature[64], hidden[24], gain[32];
    float window[256], twr[128], twi[128];
} aegis_ml_state;

void aegis_ml_init(aegis_ml_state *s);
void aegis_ml_process(aegis_ml_state *s, const float input[128], float output[128]);
void aegis_ml_predict(const float feature[64], float gain[32]);
#endif
