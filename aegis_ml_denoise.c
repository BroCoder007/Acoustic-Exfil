/* Standalone optional denoiser. Requires math library and trained model header.
 * Portable C99, FP32, no heap allocations or STM32 register dependencies.
 * See MODEL_CARD.md before deploying or interpreting these research weights.
 */
#include "aegis_ml_denoise.h"
#include "aegis_ml_model_data.h"
#include <math.h>
#include <string.h>

#define ML_PI 3.14159265358979323846f

static void fft(aegis_ml_state *s, int inverse)
{
    uint32_t j = 0;
    for (uint32_t i = 1; i < 256u; i++) {
        uint32_t bit = 128u;
        while (j & bit) { j ^= bit; bit >>= 1; }
        j ^= bit;
        if (i < j) {
            float t = s->re[i]; s->re[i] = s->re[j]; s->re[j] = t;
            t = s->im[i]; s->im[i] = s->im[j]; s->im[j] = t;
        }
    }
    for (uint32_t len = 2; len <= 256u; len <<= 1) {
        uint32_t half = len / 2u, step = 256u / len;
        for (uint32_t base = 0; base < 256u; base += len) {
            for (uint32_t k = 0; k < half; k++) {
                float wr = s->twr[k * step];
                float wi = inverse ? -s->twi[k * step] : s->twi[k * step];
                uint32_t a = base + k, b = a + half;
                float tr = wr * s->re[b] - wi * s->im[b];
                float ti = wr * s->im[b] + wi * s->re[b];
                s->re[b] = s->re[a] - tr; s->im[b] = s->im[a] - ti;
                s->re[a] += tr; s->im[a] += ti;
            }
        }
    }
    if (inverse) {
        for (uint32_t i = 0; i < 256u; i++) { s->re[i] /= 256.0f; s->im[i] /= 256.0f; }
    }
}

void aegis_ml_init(aegis_ml_state *s)
{
    memset(s, 0, sizeof *s);
    for (uint32_t n = 0; n < 256u; n++) s->window[n] = sinf(ML_PI * ((float)n + .5f) / 256.0f);
    for (uint32_t n = 0; n < 128u; n++) {
        float angle = -2.0f * ML_PI * (float)n / 256.0f;
        s->twr[n] = cosf(angle); s->twi[n] = sinf(angle);
    }
}

static void predict_internal(const float feature[64], float gain[32], float hidden[24])
{
    float normalized[64];
    for (uint32_t i = 0; i < 64u; i++)
        normalized[i] = (feature[i] - aegis_ml_mean[i]) * aegis_ml_inv_scale[i];
    for (uint32_t j = 0; j < 24u; j++) {
        float sum = aegis_ml_b1[j];
        for (uint32_t i = 0; i < 64u; i++) {
            sum += normalized[i] * aegis_ml_w1[j * 64u + i];
        }
        hidden[j] = sum > 0.0f ? sum : 0.0f;
    }
    for (uint32_t j = 0; j < 32u; j++) {
        float sum = aegis_ml_b2[j];
        for (uint32_t i = 0; i < 24u; i++) sum += hidden[i] * aegis_ml_w2[j * 24u + i];
        if (sum > 30.0f) sum = 30.0f;
        if (sum < -30.0f) sum = -30.0f;
        float g = 1.0f / (1.0f + expf(-sum));
        gain[j] = g > .05f ? g : .05f;
    }
}

void aegis_ml_predict(const float feature[64], float gain[32])
{
    float hidden[24];
    predict_internal(feature, gain, hidden);
}

void aegis_ml_process(aegis_ml_state *s, const float input[128], float output[128])
{
    memmove(s->history, s->history + 128, 128u * sizeof(float));
    memcpy(s->history + 128, input, 128u * sizeof(float));
    for (uint32_t i = 0; i < 256u; i++) {
        s->re[i] = s->history[i] * s->window[i]; s->im[i] = 0.0f;
    }
    fft(s, 0);
    float mean = 0.0f;
    for (uint32_t i = 0; i < 32u; i++) mean += s->re[i] * s->re[i] + s->im[i] * s->im[i];
    mean = mean / 32.0f + 1e-12f;
    for (uint32_t i = 0; i < 32u; i++) {
        float p = s->re[i] * s->re[i] + s->im[i] * s->im[i];
        float f = logf(1.0f + p / mean);
        s->feature[i] = f; s->feature[32u + i] = s->previous[i]; s->previous[i] = f;
    }
    predict_internal(s->feature, s->gain, s->hidden);
    for (uint32_t i = 0; i <= 128u; i++) {
        float g = i < 32u ? s->gain[i] : 0.0f;
        s->re[i] *= g; s->im[i] *= g;
        if (i > 0u && i < 128u) {
            s->re[256u - i] = s->re[i]; s->im[256u - i] = -s->im[i];
        }
    }
    fft(s, 1);
    for (uint32_t i = 0; i < 128u; i++) {
        output[i] = s->overlap[i] + s->re[i] * s->window[i];
        s->overlap[i] = s->re[128u + i] * s->window[128u + i];
    }
}
