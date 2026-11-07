/* AegisAcoustic integrated ML receiver (canonical project entry point).
 * Build main.c + aegis_ml_denoise.c + aegis_ml_adapter.c from this folder.
 * Setup: FIRST_TIME_SETUP.md. Rollback: original/main-before-ml.c.
 */
#ifndef AEGIS_ENABLE_ML
#define AEGIS_ENABLE_ML 1
#endif
#ifndef AEGIS_CLOCK_84MHZ
#define AEGIS_CLOCK_84MHZ 1
#endif
#if AEGIS_ENABLE_ML
#include "aegis_ml_adapter.h"
#endif

/*
 * AegisAcoustic - receive a message sent as sound, show it on the OLED and send it to a phone over Bluetooth (HC-05).
 * STM32F401CCU6 (Black Pill), Keil uVision 5, bare-metal + ML front end.
 *
 * The PC plays a message as two tones: bit 0 = F0_HZ, bit 1 = F1_HZ (60 ms per bit).
 * The microphone hears it, a timer triggers the ADC 40 000 times a second, the decoder rebuilds the text,
 * and the OLED shows it.  Plain messages and AES-128-CTR encrypted messages (line 6 says AES: for those).
 * Every valid message is also sent to the HC-05 over USART1 (9600 baud) as a text line.
 *
 * Wiring:
 *   mic OUT -> 2k resistor -> PA0        OLED SCL -> PB8, SDA -> PB9, VCC -> 3V3, GND -> G
 *   HC-05 TX -> PA10 (STM32 RX)          HC-05 RX -> PA9 (STM32 TX)
 *   HC-05 GND -> G                       HC-05 VCC -> 5V (module with regulator) or 3V3 (bare module)
 *
 * Frequencies: F0_HZ / F1_HZ below MUST match the PC sender profile.
 *   audible    (default here; python ultrasonic_tx.py message --profile audible)  ->  2000 /  3000
 *   ultrasonic (set USE_ULTRASONIC 1 below; python ultrasonic_tx.py message --profile ultrasonic) -> 18000 / 19000
 *
 * Clock: PLL to 84 MHz from the board's 25 MHz crystal (XTAL), or internal 16 MHz oscillator (RC).
 *        If PLL startup fails, the title and Bluetooth report the slower fallback clock.
 *
 * Last screen line:  DC = average microphone reading (about 2048),  PP = how much the sound moves (shout and it rises),
 *                    DR = lost samples / ADC overruns (must stay 0).
 *
 * Phone (Android): pair "HC-05" (PIN 1234 or 0000) in system Bluetooth settings, then open the app
 *                  "Serial Bluetooth Terminal" -> Devices -> Bluetooth classic -> HC-05.
 *
 * Keil: device STM32F401CCUx, tick CMSIS->CORE and Device->Startup, Target options:
 *       Floating Point Hardware = Single Precision.
 */
#ifndef DM_HOST_TEST
#include "stm32f4xx.h"
#endif

#if !defined(__ARM_FP) && !defined(DM_HOST_TEST)
#error "Set: Options for Target -> Target tab -> Floating Point Hardware = Single Precision (without it the decoder is far too slow)"
#endif
#include <math.h>
#include <stdint.h>
#include <string.h>

#define DM_FS          40000u
#define DM_HOP         150u                      /* samples per hop        */
#define DM_HOPS_SYM    16u                       /* hops per bit (60 ms)   */
#define DM_SYM_N       (DM_HOP * DM_HOPS_SYM)    /* 2400 samples per bit   */
#define DM_MAX_PAYLOAD 255u                     /* one-byte wire length, same as PC sender */
#define DM_WINDOW_HOPS 8u                       /* center 30 ms of each 60 ms symbol */
#define DM_PATTERN     0xAAAAD391u
#define DM_PATTERN_MASK 0x7FFFFFFFu              /* ignore the oldest bit: it may straddle the signal start */
#define DM_MAXBITS     (16u + 32u + 8u * DM_MAX_PAYLOAD + 16u)   /* mode+len, nonce, payload, CRC */

#define DM_TWO_PI 6.28318530718f

/* ================= AES-128 in counter mode (CTR) =================
 * Same scheme as the PC sender: counter block = 4-byte nonce (big-endian) followed by 12 zero bytes,
 * counted up big-endian for every 16 bytes.  Only the AES "encrypt" step is needed, for both directions.
 * The key below is a public DEMO key: change it here AND on the PC (--key) for anything real. */
static const uint8_t aes_demo_key[16] = {0x00,0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x88,0x99,0xaa,0xbb,0xcc,0xdd,0xee,0xff};
static uint8_t aes_sbox[256];
static uint8_t aes_rk[176];                     /* expanded key: 11 round keys of 16 bytes */

static uint8_t aes_xt(uint8_t x) { return (uint8_t)((x << 1) ^ ((x >> 7) * 0x1b)); }

static void aes_sbox_init(void)                 /* builds the S-box instead of storing a 256-byte table */
{
    uint8_t p = 1, q = 1;
    do {
        p = (uint8_t)(p ^ (uint8_t)(p << 1) ^ ((p & 0x80u) ? 0x1Bu : 0u));      /* p * 3 */
        q ^= (uint8_t)(q << 1); q ^= (uint8_t)(q << 2); q ^= (uint8_t)(q << 4);   /* q / 3 */
        if (q & 0x80u) q ^= 0x09u;
        uint8_t x = (uint8_t)(q ^ (uint8_t)((q << 1) | (q >> 7)) ^ (uint8_t)((q << 2) | (q >> 6))
                                ^ (uint8_t)((q << 3) | (q >> 5)) ^ (uint8_t)((q << 4) | (q >> 4)));
        aes_sbox[p] = (uint8_t)(x ^ 0x63u);
    } while (p != 1u);
    aes_sbox[0] = 0x63u;
}

static void aes_set_key(const uint8_t *key)
{
    static const uint8_t rcon[10] = {0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36};
    for (uint32_t i = 0; i < 16u; i++) aes_rk[i] = key[i];
    for (uint32_t i = 16u; i < 176u; i += 4u) {
        uint8_t t0 = aes_rk[i - 4], t1 = aes_rk[i - 3], t2 = aes_rk[i - 2], t3 = aes_rk[i - 1];
        if ((i & 15u) == 0u) {
            uint8_t tmp = t0;
            t0 = (uint8_t)(aes_sbox[t1] ^ rcon[i / 16u - 1u]); t1 = aes_sbox[t2]; t2 = aes_sbox[t3]; t3 = aes_sbox[tmp];
        }
        aes_rk[i] = (uint8_t)(aes_rk[i - 16] ^ t0);     aes_rk[i + 1] = (uint8_t)(aes_rk[i - 15] ^ t1);
        aes_rk[i + 2] = (uint8_t)(aes_rk[i - 14] ^ t2); aes_rk[i + 3] = (uint8_t)(aes_rk[i - 13] ^ t3);
    }
}

static void aes_encrypt_block(const uint8_t *in, uint8_t *out)
{
    uint8_t s[16], t[16];
    for (uint32_t i = 0; i < 16u; i++) s[i] = (uint8_t)(in[i] ^ aes_rk[i]);
    for (uint32_t round = 1; round <= 10u; round++) {
        for (uint32_t i = 0; i < 16u; i++) t[i] = aes_sbox[s[i]];                        /* SubBytes */
        for (uint32_t c = 0; c < 4u; c++)                                                /* ShiftRows */
            for (uint32_t r = 0; r < 4u; r++) s[r + 4u * c] = t[r + 4u * ((c + r) & 3u)];
        if (round < 10u) {                                                               /* MixColumns */
            for (uint32_t c = 0; c < 4u; c++) {
                uint8_t a0 = s[4u * c], a1 = s[4u * c + 1], a2 = s[4u * c + 2], a3 = s[4u * c + 3];
                s[4u * c]     = (uint8_t)(aes_xt(a0) ^ (aes_xt(a1) ^ a1) ^ a2 ^ a3);
                s[4u * c + 1] = (uint8_t)(a0 ^ aes_xt(a1) ^ (aes_xt(a2) ^ a2) ^ a3);
                s[4u * c + 2] = (uint8_t)(a0 ^ a1 ^ aes_xt(a2) ^ (aes_xt(a3) ^ a3));
                s[4u * c + 3] = (uint8_t)((aes_xt(a0) ^ a0) ^ a1 ^ a2 ^ aes_xt(a3));
            }
        }
        for (uint32_t i = 0; i < 16u; i++) s[i] = (uint8_t)(s[i] ^ aes_rk[16u * round + i]);   /* AddRoundKey */
    }
    for (uint32_t i = 0; i < 16u; i++) out[i] = s[i];
}

static void aes_ctr_crypt(uint8_t *data, uint32_t len, uint32_t nonce)
{
    uint8_t ctr[16], ks[16];
    for (uint32_t i = 0; i < 16u; i++) ctr[i] = 0;
    ctr[0] = (uint8_t)(nonce >> 24); ctr[1] = (uint8_t)(nonce >> 16); ctr[2] = (uint8_t)(nonce >> 8); ctr[3] = (uint8_t)nonce;
    for (uint32_t off = 0; off < len; off += 16u) {
        aes_encrypt_block(ctr, ks);
        for (uint32_t i = 0; i < 16u && off + i < len; i++) data[off + i] = (uint8_t)(data[off + i] ^ ks[i]);
        for (int j = 15; j >= 0; j--) { if (++ctr[j] != 0u) break; }
    }
}

enum { DM_ERROR_NONE, DM_ERROR_HEADER, DM_ERROR_WEAK, DM_ERROR_CRC, DM_ERROR_GAP };

typedef struct {
    /* configuration */
    float f0, f1, amp_min;
    /* tables: e^{-j w n} for n = 0 .. HOP-1 */
    float c0[DM_HOP], s0[DM_HOP], c1[DM_HOP], s1[DM_HOP];
    /* running state */
    float dc;
    uint32_t n_in_hop;
    float a0r, a0i, a1r, a1i;                   /* accumulators for the current hop */
    float p0[DM_WINDOW_HOPS], p1[DM_WINDOW_HOPS]; /* noncoherent hop powers */
    uint32_t hop_count;
    uint32_t sr[DM_HOPS_SYM];                   /* last bits seen on each alignment */
    uint32_t present_sr[DM_HOPS_SYM];           /* amplitude evidence for the same history */
    float sc[DM_HOPS_SYM];                      /* recent decision strength on each alignment */
    int cand_active;                            /* a sync match was seen; waiting to pick the best alignment */
    uint32_t cand_wait, cand_phase;
    float cand_sc;
    int locked;
    uint32_t lock_phase;
    uint32_t next_hop;
    uint8_t weak_count, edge_bit, edge_valid, edge_run;
    uint16_t bitcount;
    uint16_t expected_bits, failed_bits, failed_expected;
    uint8_t last_error;
    uint8_t bits[DM_MAXBITS];
    /* outputs */
    float amp0, amp1;                           /* tone amplitudes in ADC counts, latest symbol window */
    uint32_t frames_ok, frames_bad;
    char text[DM_MAX_PAYLOAD + 1];
    uint8_t text_len;
    uint8_t mode;                               /* 0 = plain message, 1 = AES-decrypted message */
    uint8_t new_msg;                            /* set to 1 when a valid frame arrived; clear it after reading */
} dm_t;

static inline uint16_t dm_crc16(const uint8_t *d, uint32_t n)
{
    uint16_t crc = 0xFFFFu;
    for (uint32_t i = 0; i < n; i++) {
        crc ^= (uint16_t)((uint16_t)d[i] << 8);
        for (int k = 0; k < 8; k++) crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u) : (uint16_t)(crc << 1);
    }
    return crc;
}

static inline void dm_init(dm_t *d, float f0, float f1, float amp_min)
{
    memset(d, 0, sizeof *d);
    d->f0 = f0; d->f1 = f1; d->amp_min = amp_min;
    for (uint32_t n = 0; n < DM_HOP; n++) {
        float a0 = DM_TWO_PI * f0 * (float)n / (float)DM_FS;
        float a1 = DM_TWO_PI * f1 * (float)n / (float)DM_FS;
        d->c0[n] = cosf(a0); d->s0[n] = -sinf(a0);
        d->c1[n] = cosf(a1); d->s1[n] = -sinf(a1);
    }
    d->dc = 2048.0f;
}

/* Reset frame acquisition without erasing the last validated output. */
static void dm_unlock(dm_t *d)
{
    d->locked = 0; d->cand_active = 0; d->bitcount = 0;
    d->expected_bits = 0;
    d->weak_count = 0; d->edge_valid = 0; d->edge_run = 0;
    memset(d->sr, 0, sizeof d->sr); memset(d->sc, 0, sizeof d->sc);
    memset(d->present_sr, 0, sizeof d->present_sr);
}

static void dm_reject(dm_t *d, uint8_t reason)
{
    d->failed_bits = d->bitcount; d->failed_expected = d->expected_bits;
    d->last_error = reason; d->frames_bad++;
    dm_unlock(d);
}

static const char *dm_error_text(uint8_t reason)
{
    switch (reason) {
    case DM_ERROR_HEADER: return "RX ERROR: invalid mode/length header\r\n";
    case DM_ERROR_WEAK: return "RX ERROR: signal lost or too weak\r\n";
    case DM_ERROR_CRC: return "RX ERROR: CRC mismatch; audio bits corrupted\r\n";
    case DM_ERROR_GAP: return "RX ERROR: ADC/sample buffer gap\r\n";
    default: return "RX ERROR: unknown\r\n";
    }
}

/* A lost sample invalidates timing: discard history and acquire a fresh preamble. */
static void dm_stream_gap(dm_t *d)
{
    if (d->locked || d->cand_active) dm_reject(d, DM_ERROR_GAP);
    else dm_unlock(d);
    d->n_in_hop = 0; d->hop_count = 0;
    d->a0r = d->a0i = d->a1r = d->a1i = 0.0f;
    memset(d->p0, 0, sizeof d->p0); memset(d->p1, 0, sizeof d->p1);
}

static void dm_accept_bit(dm_t *d, uint32_t bit)
{
    if (d->bitcount < DM_MAXBITS) d->bits[d->bitcount++] = (uint8_t)bit;

    if (d->bitcount >= 16u) {
        uint8_t mode = 0, len = 0;
        for (int i = 0; i < 8; i++) { mode = (uint8_t)((mode << 1) | d->bits[i]); len = (uint8_t)((len << 1) | d->bits[8 + i]); }
        if (mode > 1u || len < 1u || len > DM_MAX_PAYLOAD) {
            dm_reject(d, DM_ERROR_HEADER); return;
        }
        uint16_t pstart = (uint16_t)(16u + (mode ? 32u : 0u));          /* where the payload bits begin */
        uint16_t need = (uint16_t)(pstart + 8u * len + 16u);
        d->expected_bits = need;
        if (d->bitcount == need) {
            uint8_t buf[2 + 4 + DM_MAX_PAYLOAD];
            uint32_t nb = 2, nonce = 0;
            buf[0] = mode; buf[1] = len;
            if (mode) {
                for (uint32_t i = 0; i < 4u; i++) {
                    uint8_t v = 0;
                    for (int k = 0; k < 8; k++) v = (uint8_t)((v << 1) | d->bits[16 + 8 * i + k]);
                    buf[nb++] = v; nonce = (nonce << 8) | v;
                }
            }
            for (uint32_t i = 0; i < len; i++) {
                uint8_t v = 0;
                for (int k = 0; k < 8; k++) v = (uint8_t)((v << 1) | d->bits[pstart + 8 * i + k]);
                buf[nb + i] = v;
            }
            uint16_t crc_rx = 0;
            for (int k = 0; k < 16; k++) crc_rx = (uint16_t)((crc_rx << 1) | d->bits[pstart + 8 * len + k]);
            if (crc_rx == dm_crc16(buf, nb + len)) {                      /* CRC covers mode, len, nonce, payload as sent */
                uint8_t *pay = buf + nb;
                if (mode) aes_ctr_crypt(pay, len, nonce);                 /* decrypt */
                for (uint32_t i = 0; i < len; i++) d->text[i] = (char)pay[i];
                d->text[len] = 0; d->text_len = len; d->mode = mode; d->new_msg = 1; d->frames_ok++;
            } else {
                dm_reject(d, DM_ERROR_CRC); return;
            }
            dm_unlock(d);
        }
    }
}

/* Called once per 3.75 ms hop. Short independent correlations tolerate
 * carrier offset without cancellation across an entire 60 ms symbol. */
static inline void dm_hop_done(dm_t *d)
{
    uint32_t h = d->hop_count++;
    uint32_t slot = h % DM_WINDOW_HOPS;
    float p0 = d->a0r * d->a0r + d->a0i * d->a0i;
    float p1 = d->a1r * d->a1r + d->a1i * d->a1i;
    d->p0[slot] = p0; d->p1[slot] = p1;
    d->a0r = d->a0i = d->a1r = d->a1i = 0.0f;
    if (d->hop_count < DM_WINDOW_HOPS) return;

    float sum0 = 0.0f, sum1 = 0.0f;
    for (uint32_t k = 0; k < DM_WINDOW_HOPS; k++) {
        sum0 += d->p0[k]; sum1 += d->p1[k];
    }
    float A0 = 2.0f * sqrtf(sum0 / (float)DM_WINDOW_HOPS) / (float)DM_HOP;
    float A1 = 2.0f * sqrtf(sum1 / (float)DM_WINDOW_HOPS) / (float)DM_HOP;
    d->amp0 = A0; d->amp1 = A1;
    uint32_t bit = (sum1 > sum0) ? 1u : 0u;
    float strong = A0 > A1 ? A0 : A1;
    float confidence = fabsf(sum1 - sum0) / (sum0 + sum1 + 1.0f);
    uint32_t phase = h % DM_HOPS_SYM;

    if (!d->locked) {
        /* A low-confidence bit is still evidence. Clearing 31 bits of sync
         * history on a single uncertain window prevented lock under fan noise.
         * Require the exact sync word plus amplitude/average-quality evidence. */
        d->sr[phase] = (d->sr[phase] << 1) | bit;
        d->present_sr[phase] = (d->present_sr[phase] << 1) | (strong > d->amp_min ? 1u : 0u);
        d->sc[phase] = 0.95f * d->sc[phase] + 0.05f * confidence;
        if ((d->sr[phase] & DM_PATTERN_MASK) == (DM_PATTERN & DM_PATTERN_MASK) && d->sc[phase] > 0.16f) {
            uint32_t evidence = d->present_sr[phase] & DM_PATTERN_MASK, count = 0;
            while (evidence) { evidence &= evidence - 1u; count++; }
            if (count >= 26u) {
                if (!d->cand_active) {
                    d->cand_active = 1; d->cand_wait = DM_HOPS_SYM - 1u;
                    d->cand_phase = phase; d->cand_sc = d->sc[phase];
                } else if (d->sc[phase] > d->cand_sc) {
                    d->cand_phase = phase; d->cand_sc = d->sc[phase];
                }
            }
        }
        if (d->cand_active) {
            if (d->cand_wait == 0u) {
                d->cand_active = 0; d->locked = 1; d->bitcount = 0; d->expected_bits = 0;
                d->lock_phase = d->cand_phase;
                /* First selected-phase occurrence strictly after this hop.
                 * Candidate collection ends before any selected phase's next bit. */
                d->next_hop = h + 1u + ((d->lock_phase + DM_HOPS_SYM -
                                  ((h + 1u) % DM_HOPS_SYM)) % DM_HOPS_SYM);
                d->weak_count = 0; d->edge_valid = 0; d->edge_run = 0;
            } else d->cand_wait--;
        }
        return;
    }

    /* Track real bit transitions using two confident short hops. Correct the
     * decision time by at most one hop per edge; only edges near the predicted
     * boundary may steer it. This prevents clock error accumulating for minutes. */
    float edge_conf = fabsf(p1 - p0) / (p0 + p1 + 1.0f);
    float floor_power = 0.25f * d->amp_min * d->amp_min * (float)(DM_HOP * DM_HOP);
    if ((p0 > p1 ? p0 : p1) > floor_power && edge_conf > 0.65f) {
        uint8_t eb = p1 > p0 ? 1u : 0u;
        if (d->edge_run && eb == d->edge_bit) {
            if (d->edge_run == 1u && d->edge_valid) {
                /* h is the second stable hop; the first ended one hop after
                 * the boundary. End the center window 12 hops after that boundary. */
                uint32_t target = h + DM_HOPS_SYM - DM_WINDOW_HOPS / 2u - 2u;
                int32_t error = (int32_t)(target - d->next_hop);
                if (error >= -7 && error <= 7) {
                    if (error > 0) d->next_hop++;
                    if (error < 0) d->next_hop--;
                }
            }
            d->edge_run = 2u;
        } else {
            if (d->edge_run) d->edge_valid = 1u;
            d->edge_bit = eb; d->edge_run = 1u;
        }
    } else {
        /* Keep the previous stable value across a mixed transition hop. */
    }
    if (h != d->next_hop) return;
    d->next_hop += DM_HOPS_SYM;
    /* One quiet decision need not destroy sync. CRC still decides validity. */
    if (strong < d->amp_min * 0.35f || confidence < 0.10f) {
        if (++d->weak_count >= 3u) { dm_reject(d, DM_ERROR_WEAK); return; }
    } else d->weak_count = 0;
    dm_accept_bit(d, bit);
}

/* feed one ADC sample (0..4095) */
static inline void dm_push(dm_t *d, uint16_t x)
{
    float y = (float)x - d->dc;
    d->dc += y * (1.0f / 256.0f);                      /* remove the DC bias (slow tracking) */
    uint32_t n = d->n_in_hop;
    d->a0r += y * d->c0[n]; d->a0i += y * d->s0[n];
    d->a1r += y * d->c1[n]; d->a1i += y * d->s1[n];
    if (++d->n_in_hop >= DM_HOP) { d->n_in_hop = 0; dm_hop_done(d); }
}

/* Bluetooth queue: publish complete lines, never silently truncate a message.
 * Queue producer and consumer both run in the foreground main loop. */
#define BT_N 512u
static char btq[BT_N];
static uint32_t bt_h, bt_t;
static int bt_enqueue(const char *data, uint32_t len)
{
    if (len > BT_N - (bt_h - bt_t)) return 0;
    for (uint32_t i = 0; i < len; i++) btq[(bt_h + i) & (BT_N - 1u)] = data[i];
    bt_h += len;
    return 1;
}
static int bt_puts(const char *s) { return bt_enqueue(s, (uint32_t)strlen(s)); }
static int bt_queue_message(const dm_t *d)
{
    char line[DM_MAX_PAYLOAD + 7u];
    const char *prefix = d->mode ? "AES: " : "RX: ";
    uint32_t n = (uint32_t)strlen(prefix);
    memcpy(line, prefix, n);
    memcpy(line + n, d->text, d->text_len); n += d->text_len;
    line[n++] = '\r'; line[n++] = '\n';
    return bt_enqueue(line, n);
}

static char *bt_append_uint(char *p, uint32_t value)
{
    char digits[10]; uint32_t n = 0;
    do { digits[n++] = (char)('0' + value % 10u); value /= 10u; } while (value);
    while (n) *p++ = digits[--n];
    return p;
}

static int bt_report_progress(const dm_t *d)
{
    char line[64], *p = line;
    const char *s = "AEGIS ALIVE | RX ";
    while (*s) *p++ = *s++;
    p = bt_append_uint(p, d->bitcount); *p++ = '/';
    if (d->expected_bits) p = bt_append_uint(p, d->expected_bits);
    else *p++ = '?';
    s = " bits\r\n"; while (*s) *p++ = *s++;
    return bt_enqueue(line, (uint32_t)(p - line));
}

/* Same foreground sample path is used by the board and the host validation.
 * ADC interrupt only queues samples. All FFT/model/decoder work stays here. */
static dm_t dm;
static uint32_t receiver_ml_blocks, receiver_ml_peak_cycles;
#if AEGIS_ENABLE_ML
static aegis_ml_adapter receiver_filter;
static uint16_t receiver_filtered[AEGIS_ML_HOP];
#endif

static void receiver_path_init(float f0, float f1, float amp_min)
{
    dm_init(&dm, f0, f1, amp_min);
    receiver_ml_blocks = receiver_ml_peak_cycles = 0;
#if AEGIS_ENABLE_ML
    aegis_ml_adapter_init(&receiver_filter);
#endif
}

static void receiver_path_gap(void)
{
    dm_stream_gap(&dm);
#if AEGIS_ENABLE_ML
    /* Never combine filter history across a lost ADC sample. */
    aegis_ml_adapter_init(&receiver_filter);
#endif
}

static void receiver_path_push(uint16_t adc)
{
#if AEGIS_ENABLE_ML
#ifndef DM_HOST_TEST
    uint32_t start = 0;
    if (receiver_filter.used == AEGIS_ML_HOP - 1u) start = DWT->CYCCNT;
#endif
    uint32_t count = aegis_ml_adapter_push(&receiver_filter, adc, receiver_filtered);
    if (count) {
        for (uint32_t i = 0; i < count; i++) dm_push(&dm, receiver_filtered[i]);
        receiver_ml_blocks++;
#ifndef DM_HOST_TEST
        /* Includes filter + decoder burst and any interrupt time during it. */
        uint32_t elapsed = DWT->CYCCNT - start;
        if (elapsed > receiver_ml_peak_cycles) receiver_ml_peak_cycles = elapsed;
#endif
    }
#else
    dm_push(&dm, adc);
#endif
}

static uint32_t receiver_peak_us(uint32_t clock)
{
    uint32_t cycles_per_us = clock / 1000000u;
    return cycles_per_us ? receiver_ml_peak_cycles / cycles_per_us : 0;
}

static int receiver_ml_slow(uint32_t clock)
{
    /* Each 128-sample block arrives in 3200 us. Reserve 25% for other work.
     * This is a warning threshold, not a measurement of total CPU utilization. */
    return receiver_ml_peak_cycles > (clock / DM_FS) * 128u * 3u / 4u;
}

static int receiver_report_status(uint32_t clock, uint32_t lost)
{
    char line[128], *p = line;
#if AEGIS_ENABLE_ML
    const char *s = receiver_ml_slow(clock) ? "AEGIS ALIVE ML SLOW CLK=" : "AEGIS ALIVE ML ON CLK=";
#else
    const char *s = "AEGIS ALIVE ML BYPASS CLK=";
#endif
    while (*s) *p++ = *s++;
    p = bt_append_uint(p, clock / 1000000u);
    s = "M MAX_US="; while (*s) *p++ = *s++;
    p = bt_append_uint(p, receiver_peak_us(clock));
    s = " DR="; while (*s) *p++ = *s++;
    p = bt_append_uint(p, lost);
    s = " RX="; while (*s) *p++ = *s++;
    p = bt_append_uint(p, dm.bitcount); *p++ = '/';
    if (dm.expected_bits) p = bt_append_uint(p, dm.expected_bits);
    else *p++ = '?';
    *p++ = '\r'; *p++ = '\n';
    return bt_enqueue(line, (uint32_t)(p - line));
}

#ifndef DM_HOST_TEST
/* ================= firmware ================= */
/* Must match the PC sender profile.
 *   USE_ULTRASONIC 0 -> 2000/3000 Hz   (python ultrasonic_tx.py message --profile audible)   <- default, bars react to normal sound
 *   USE_ULTRASONIC 1 -> 18000/19000 Hz (python ultrasonic_tx.py message --profile ultrasonic) <- silent room = numbers stay near 0 */
#ifndef USE_ULTRASONIC
#define USE_ULTRASONIC 0
#endif
#if USE_ULTRASONIC
  #define F0_HZ 18000
  #define F1_HZ 19000
#else
  #define F0_HZ 2000
  #define F1_HZ 3000
#endif
#ifndef AMP_MIN
#define AMP_MIN 4.0f
#endif
#if AEGIS_ENABLE_ML && USE_ULTRASONIC
#error "Trained model requires USE_ULTRASONIC=0 (2000/3000 Hz)"
#endif
#define OLED_ADDR 0x3Cu
#define BT_BAUD   9600u                          /* HC-05 data-mode default */

/* ------------------------------------------------------------ time */
static volatile uint32_t ms;
void SysTick_Handler(void) { ms++; }
static void pc13(int on) { GPIOC->BSRR = on ? (1u << (13 + 16)) : (1u << 13); }   /* active low */

/* ------------------------------------------------------------ sample ring buffer (filled at 40 kHz) */
#define RING_N 8192u
static volatile uint16_t ring[RING_N];
static volatile uint32_t head, tail;
static volatile uint32_t dropped, adc_overruns;

static inline void ring_put(uint16_t v)
{
    if ((head - tail) >= RING_N) { dropped++; return; }
    ring[head & (RING_N - 1u)] = v;
    head++;
}

void ADC_IRQHandler(void)
{
    uint32_t status = ADC1->SR;
    if (status & ADC_SR_OVR) adc_overruns++;
    if (status & ADC_SR_EOC) ring_put((uint16_t)ADC1->DR);       /* reading DR clears EOC */
    ADC1->SR = 0;
}

/* ------------------------------------------------------------ Bluetooth (HC-05 on USART1: PA9 TX, PA10 RX) */
/* Bytes go into a queue and are sent one per pass of the main loop, so the microphone samples are never left waiting. */
static void bt_service(void)
{
    if (USART1->SR & USART_SR_RXNE) {                  /* echo anything typed in the phone app (tests the phone -> board direction) */
        char c = (char)USART1->DR;
        (void)bt_enqueue(&c, 1u);
    }
    if (bt_t != bt_h && (USART1->SR & USART_SR_TXE)) USART1->DR = (uint8_t)btq[bt_t++ & (BT_N - 1u)];
}

/* ------------------------------------------------------------ I2C + OLED */
static int wait_sr1(uint32_t flag)
{
    uint32_t t = 100000u;
    while (!(I2C1->SR1 & flag)) {
        if (I2C1->SR1 & I2C_SR1_AF) { I2C1->SR1 &= ~I2C_SR1_AF; return 0; }
        if (--t == 0u) return 0;
    }
    return 1;
}
static int oled_write(uint8_t control, const uint8_t *d, uint32_t n)
{
    I2C1->CR1 |= I2C_CR1_START;
    if (!wait_sr1(I2C_SR1_SB)) return 0;
    I2C1->DR = (uint32_t)(OLED_ADDR << 1);
    if (!wait_sr1(I2C_SR1_ADDR)) { I2C1->CR1 |= I2C_CR1_STOP; return 0; }
    (void)I2C1->SR1; (void)I2C1->SR2;
    I2C1->DR = control;
    if (!wait_sr1(I2C_SR1_TXE)) { I2C1->CR1 |= I2C_CR1_STOP; return 0; }
    for (uint32_t i = 0; i < n; i++) {
        I2C1->DR = d[i];
        if (!wait_sr1(I2C_SR1_TXE)) { I2C1->CR1 |= I2C_CR1_STOP; return 0; }
    }
    if (!wait_sr1(I2C_SR1_BTF)) { I2C1->CR1 |= I2C_CR1_STOP; return 0; }
    I2C1->CR1 |= I2C_CR1_STOP;
    return 1;
}
static int oled_cmd(uint8_t c) { return oled_write(0x00, &c, 1); }
static int oled_init(void)
{
    static const uint8_t init[] = {
        0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40, 0x8D, 0x14, 0x20, 0x00,
        0xA1, 0xC8, 0xDA, 0x12, 0x81, 0xCF, 0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6, 0xAF
    };
    for (uint32_t i = 0; i < sizeof init; i++)
        if (!oled_cmd(init[i])) return 0;
    return 1;
}

static uint8_t fb[128 * 8];
static const uint8_t font5x7[][5] = {
 {0x00,0x00,0x00,0x00,0x00},{0x00,0x00,0x5F,0x00,0x00},{0x00,0x07,0x00,0x07,0x00},{0x14,0x7F,0x14,0x7F,0x14},
 {0x24,0x2A,0x7F,0x2A,0x12},{0x23,0x13,0x08,0x64,0x62},{0x36,0x49,0x56,0x20,0x50},{0x00,0x05,0x03,0x00,0x00},
 {0x00,0x1C,0x22,0x41,0x00},{0x00,0x41,0x22,0x1C,0x00},{0x2A,0x1C,0x7F,0x1C,0x2A},{0x08,0x08,0x3E,0x08,0x08},
 {0x00,0x50,0x30,0x00,0x00},{0x08,0x08,0x08,0x08,0x08},{0x00,0x60,0x60,0x00,0x00},{0x20,0x10,0x08,0x04,0x02},
 {0x3E,0x51,0x49,0x45,0x3E},{0x00,0x42,0x7F,0x40,0x00},{0x72,0x49,0x49,0x49,0x46},{0x21,0x41,0x49,0x4D,0x33},
 {0x18,0x14,0x12,0x7F,0x10},{0x27,0x45,0x45,0x45,0x39},{0x3C,0x4A,0x49,0x49,0x31},{0x41,0x21,0x11,0x09,0x07},
 {0x36,0x49,0x49,0x49,0x36},{0x46,0x49,0x49,0x29,0x1E},{0x00,0x36,0x36,0x00,0x00},{0x00,0x56,0x36,0x00,0x00},
 {0x08,0x14,0x22,0x41,0x00},{0x14,0x14,0x14,0x14,0x14},{0x00,0x41,0x22,0x14,0x08},{0x02,0x01,0x59,0x09,0x06},
 {0x3E,0x41,0x5D,0x59,0x4E},{0x7C,0x12,0x11,0x12,0x7C},{0x7F,0x49,0x49,0x49,0x36},{0x3E,0x41,0x41,0x41,0x22},
 {0x7F,0x41,0x41,0x41,0x3E},{0x7F,0x49,0x49,0x49,0x41},{0x7F,0x09,0x09,0x09,0x01},{0x3E,0x41,0x41,0x51,0x73},
 {0x7F,0x08,0x08,0x08,0x7F},{0x00,0x41,0x7F,0x41,0x00},{0x20,0x40,0x41,0x3F,0x01},{0x7F,0x08,0x14,0x22,0x41},
 {0x7F,0x40,0x40,0x40,0x40},{0x7F,0x02,0x1C,0x02,0x7F},{0x7F,0x04,0x08,0x10,0x7F},{0x3E,0x41,0x41,0x41,0x3E},
 {0x7F,0x09,0x09,0x09,0x06},{0x3E,0x41,0x51,0x21,0x5E},{0x7F,0x09,0x19,0x29,0x46},{0x26,0x49,0x49,0x49,0x32},
 {0x03,0x01,0x7F,0x01,0x03},{0x3F,0x40,0x40,0x40,0x3F},{0x1F,0x20,0x40,0x20,0x1F},{0x3F,0x40,0x38,0x40,0x3F},
 {0x63,0x14,0x08,0x14,0x63},{0x03,0x04,0x78,0x04,0x03},{0x61,0x59,0x49,0x4D,0x43}
};
static void oled_clear(void) { for (uint32_t i = 0; i < sizeof fb; i++) fb[i] = 0; }
static void oled_text(uint8_t page, uint8_t col, const char *s)
{
    uint32_t p = (uint32_t)page * 128u + (uint32_t)col * 6u;
    while (*s && p + 6u <= (uint32_t)(page + 1u) * 128u) {
        char c = *s++;
        if (c >= 'a' && c <= 'z') c = (char)(c - 32);
        if (c < 0x20 || c > 0x5A) c = '?';
        for (int k = 0; k < 5; k++) fb[p++] = font5x7[c - 0x20][k];
        fb[p++] = 0;
    }
}
static void oled_bar(uint32_t len)                          /* pages 3..4 */
{
    if (len > 126u) len = 126u;
    for (uint32_t x = 0; x < 128u; x++) {
        uint8_t edge = (x == 0u || x == 127u) ? 0xFF : 0x81;
        fb[3u * 128u + x] = edge; fb[4u * 128u + x] = edge;
        if (x >= 1u && x <= len) { fb[3u * 128u + x] = 0xFF; fb[4u * 128u + x] = 0xFF; }
    }
}
/* The screen is sent in 64 small pieces of 16 bytes, one piece per pass of the main loop, so the microphone samples
 * are never left waiting (sending the whole screen at once blocks for about 100 ms). */
static uint8_t oled_chunk;                  /* next piece to send, 0..63 */
static int oled_sending;
static void oled_service(void)
{
    if (!oled_sending) return;
    uint8_t page = (uint8_t)(oled_chunk / 8u);
    uint8_t col  = (uint8_t)((oled_chunk % 8u) * 16u);
    if (oled_cmd(0x21) && oled_cmd(col) && oled_cmd((uint8_t)(col + 15u)) &&
        oled_cmd(0x22) && oled_cmd(page) && oled_cmd(page) &&
        oled_write(0x40, &fb[(uint32_t)page * 128u + col], 16u)) {
        if (++oled_chunk >= 64u) oled_sending = 0;
    } else {
        oled_sending = 0;                   /* give up on this picture, try again at the next refresh */
    }
}
static char *utoa_s(uint32_t v, char *buf)
{
    char t[11]; int i = 0, j = 0;
    do { t[i++] = (char)('0' + v % 10u); v /= 10u; } while (v);
    while (i) buf[j++] = t[--i];
    buf[j] = 0;
    return buf;
}

/* ------------------------------------------------------------ main */
/* dm and the shared sample path are declared above the hardware section. */
static uint32_t sysclk = 16000000u, pclk1 = 16000000u;
static int clock_hse;
static uint16_t s_min = 4095, s_max = 0;      /* raw microphone readings since the last screen update */
static uint32_t s_sum, s_cnt;

static int clock_wait(volatile uint32_t *reg, uint32_t mask, uint32_t wanted)
{
    uint32_t timeout = 4000000u;
    while ((*reg & mask) != wanted && --timeout) { }
    return (*reg & mask) == wanted;
}

static void configure_clock(void)
{
    /* First move to HSI; the startup file may have selected a PLL already. */
    RCC->CR |= RCC_CR_HSION;
    if (!clock_wait(&RCC->CR, RCC_CR_HSIRDY, RCC_CR_HSIRDY)) {
        /* Cannot safely rewrite a live PLL. Stay with startup's clock. */
        SystemCoreClockUpdate(); sysclk = SystemCoreClock;
        static const uint8_t apb_shift[8] = {0,0,0,0,1,2,3,4};
        pclk1 = sysclk >> apb_shift[(RCC->CFGR >> 10) & 7u];
        return;
    }
    RCC->CFGR &= ~RCC_CFGR_SW;
    if (!clock_wait(&RCC->CFGR, RCC_CFGR_SWS, RCC_CFGR_SWS_HSI)) {
        SystemCoreClockUpdate(); sysclk = SystemCoreClock;
        static const uint8_t apb_shift[8] = {0,0,0,0,1,2,3,4};
        pclk1 = sysclk >> apb_shift[(RCC->CFGR >> 10) & 7u];
        return;
    }
    RCC->CR &= ~RCC_CR_PLLON;
    int pll_stopped = clock_wait(&RCC->CR, RCC_CR_PLLRDY, 0);
    RCC->CFGR &= ~(RCC_CFGR_HPRE | RCC_CFGR_PPRE1 | RCC_CFGR_PPRE2);
    RCC->CR |= RCC_CR_HSEON;
    clock_hse = clock_wait(&RCC->CR, RCC_CR_HSERDY, RCC_CR_HSERDY);

#if AEGIS_CLOCK_84MHZ
    RCC->APB1ENR |= RCC_APB1ENR_PWREN;
    (void)RCC->APB1ENR;
    PWR->CR = (PWR->CR & ~PWR_CR_VOS) | PWR_CR_VOS_1; /* scale 2, 3.3 V board */
    FLASH->ACR = (FLASH->ACR & ~FLASH_ACR_LATENCY) | FLASH_ACR_LATENCY_2WS |
                 FLASH_ACR_ICEN | FLASH_ACR_DCEN | FLASH_ACR_PRFTEN;
    if (pll_stopped && clock_wait(&FLASH->ACR, FLASH_ACR_LATENCY, FLASH_ACR_LATENCY_2WS)) {
        /* 25/25 or 16/16 MHz input, VCO=336 MHz, SYSCLK=84, PLLQ=48 MHz. */
        RCC->PLLCFGR = (clock_hse ? 25u : 16u) | (336u << 6) |
                       RCC_PLLCFGR_PLLP_0 | (7u << 24) |
                       (clock_hse ? RCC_PLLCFGR_PLLSRC_HSE : 0u);
        RCC->CFGR = (RCC->CFGR & ~RCC_CFGR_PPRE1) | RCC_CFGR_PPRE1_DIV2;
        RCC->CR |= RCC_CR_PLLON;
        /* VOS is active with PLL on; wait here before switching SYSCLK. */
        if (clock_wait(&RCC->CR, RCC_CR_PLLRDY, RCC_CR_PLLRDY) &&
            clock_wait(&PWR->CSR, PWR_CSR_VOSRDY, PWR_CSR_VOSRDY)) {
            RCC->CFGR = (RCC->CFGR & ~RCC_CFGR_SW) | RCC_CFGR_SW_PLL;
            if (clock_wait(&RCC->CFGR, RCC_CFGR_SWS, RCC_CFGR_SWS_PLL)) {
                sysclk = 84000000u; pclk1 = 42000000u;
                SystemCoreClock = sysclk;
                return;
            }
        }
    }
#else
    (void)pll_stopped;
#endif
    /* Bounded startup fallback: use a direct oscillator, at the reported speed. */
    RCC->CFGR = (RCC->CFGR & ~RCC_CFGR_SW) | (clock_hse ? RCC_CFGR_SW_HSE : RCC_CFGR_SW_HSI);
    if (clock_wait(&RCC->CFGR, RCC_CFGR_SWS, clock_hse ? RCC_CFGR_SWS_HSE : RCC_CFGR_SWS_HSI)) {
        RCC->CR &= ~RCC_CR_PLLON;
        RCC->CFGR &= ~(RCC_CFGR_HPRE | RCC_CFGR_PPRE1 | RCC_CFGR_PPRE2);
        sysclk = clock_hse ? 25000000u : 16000000u; pclk1 = sysclk;
        SystemCoreClock = sysclk;
    } else {
        SystemCoreClockUpdate(); sysclk = SystemCoreClock;
        pclk1 = sysclk >> ((RCC->CFGR & RCC_CFGR_PPRE1) == RCC_CFGR_PPRE1_DIV2 ? 1u : 0u);
    }
}

int main(void)
{
    RCC->AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN | RCC_AHB1ENR_GPIOCEN;
    RCC->APB1ENR |= RCC_APB1ENR_I2C1EN | RCC_APB1ENR_TIM2EN;
    RCC->APB2ENR |= RCC_APB2ENR_ADC1EN | RCC_APB2ENR_USART1EN;                   /* ADC (microphone) and USART1 (Bluetooth) */

    configure_clock();

    GPIOC->MODER &= ~(3u << 26); GPIOC->MODER |= (1u << 26); pc13(0);
    GPIOA->MODER |= 3u;                                                           /* PA0 analog (needs no alternate function) */

    /* USART1 -> HC-05: PA9 = TX, PA10 = RX, alternate function 7.  APB2 is not divided, so its clock = sysclk. */
    GPIOA->MODER &= ~((3u << 18) | (3u << 20));
    GPIOA->MODER |=  ((2u << 18) | (2u << 20));
    GPIOA->AFR[1] &= ~((0xFu << 4) | (0xFu << 8));
    GPIOA->AFR[1] |=  ((7u << 4)   | (7u << 8));
    USART1->BRR = (sysclk + BT_BAUD / 2u) / BT_BAUD;
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;

    GPIOB->MODER  &= ~((3u << 16) | (3u << 18)); GPIOB->MODER |= ((2u << 16) | (2u << 18));
    GPIOB->OTYPER |= (1u << 8) | (1u << 9);
    GPIOB->PUPDR  &= ~((3u << 16) | (3u << 18)); GPIOB->PUPDR |= ((1u << 16) | (1u << 18));
    GPIOB->AFR[1] &= ~((0xFu << 0) | (0xFu << 4)); GPIOB->AFR[1] |= ((4u << 0) | (4u << 4));
    I2C1->CR1 = I2C_CR1_SWRST; I2C1->CR1 = 0u;
    I2C1->CR2 = pclk1 / 1000000u; I2C1->CCR = pclk1 / 200000u; I2C1->TRISE = pclk1 / 1000000u + 1u; I2C1->CR1 = I2C_CR1_PE;   /* 100 kHz */

    SCB->CPACR |= (0xFu << 20); __DSB(); __ISB();                                 /* enable the FPU */
    SysTick_Config(sysclk / 1000u);
    while (ms < 300u) { }
    int oled_ok = oled_init();
    aes_sbox_init(); aes_set_key(aes_demo_key);
    receiver_path_init((float)F0_HZ, (float)F1_HZ, AMP_MIN);
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;

    /* TIM2: APB1 timer clock = sysclk (84 MHz / 2100, or oscillator fallback). */
    TIM2->PSC = 0u; TIM2->ARR = sysclk / DM_FS - 1u;
    TIM2->CR2 = TIM_CR2_MMS_1;                                                    /* TRGO = update */
    ADC->CCR = (ADC->CCR & ~ADC_CCR_ADCPRE) | ADC_CCR_ADCPRE_0; /* PCLK2/4: 21 MHz at 84 MHz core */
    ADC1->SQR3 = 0u; ADC1->SMPR2 = 3u;                                            /* channel 0, 56 cycles */
    ADC1->CR1 = ADC_CR1_EOCIE;
    ADC1->CR2 = ADC_CR2_ADON | ADC_CR2_EXTEN_0 | ADC_CR2_EXTSEL_1 | ADC_CR2_EXTSEL_2;   /* TIM2 TRGO, rising */
    NVIC_SetPriority(ADC_IRQn, 1); NVIC_EnableIRQ(ADC_IRQn);
    TIM2->EGR = TIM_EGR_UG; TIM2->SR = 0; TIM2->CR1 = TIM_CR1_CEN;

    uint32_t t_oled = 0, t_msg = 0; int msg_led = 0;
    char b[12];
    char last[DM_MAX_PAYLOAD + 1]; last[0] = 0;
    uint8_t last_mode = 0;
    uint32_t lost_seen = 0, overrun_seen = 0, text_offset = 0, t_scroll = 0;
    uint32_t bad_seen = 0, t_error = 0; int was_locked = 0;

    receiver_report_status(sysclk, 0);

    for (;;) {
        if (dropped != lost_seen || adc_overruns != overrun_seen) {
            lost_seen = dropped; overrun_seen = adc_overruns;
            tail = head; /* queued samples span a gap; do not splice them */
            receiver_path_gap();
        }
        uint32_t n = 0;
        while (tail != head && n < 2000u) {
            uint16_t v = ring[tail & (RING_N - 1u)];
            if (v < s_min) s_min = v;
            if (v > s_max) s_max = v;
            s_sum += v; s_cnt++;
            receiver_path_push(v);
            tail++; n++;
        }

        if (dm.locked && !was_locked)
            bt_puts("RX LOCKED: preamble found; waiting for full frame\r\n");
        was_locked = dm.locked;
        if (dm.frames_bad != bad_seen) {
            if (bt_puts(dm_error_text(dm.last_error))) {
                bad_seen = dm.frames_bad; t_error = ms;
            }
        }

        if (dm.new_msg && bt_queue_message(&dm)) {
            dm.new_msg = 0;
            for (uint32_t i = 0; i <= dm.text_len; i++) last[i] = dm.text[i];
            last_mode = dm.mode; text_offset = 0; t_scroll = ms;
            msg_led = 1; t_msg = ms; pc13(1);
            /* The full prefix + payload + CRLF is already queued atomically. */
        }
        if (msg_led && (ms - t_msg) > 2000u) { msg_led = 0; pc13(0); }

        if ((ms - t_scroll) >= 800u) {
            t_scroll = ms;
            if (strlen(last) > 17u) {
                if (text_offset + 17u >= strlen(last)) text_offset = 0;
                else { text_offset += 8u; if (text_offset + 17u > strlen(last)) text_offset = strlen(last) - 17u; }
            }
        }
        if (oled_ok && !oled_sending && (ms - t_oled) >= 400u) {
            t_oled = ms;
            float strong = (dm.amp0 > dm.amp1) ? dm.amp0 : dm.amp1;
            const char *state = msg_led ? "RECEIVED" : (dm.locked ? "LOCKED" : (strong > AMP_MIN ? "TONE HEARD" : "LISTENING"));
            if (!msg_led && !dm.locked && bad_seen && (ms - t_error) < 4000u) {
                switch (dm.last_error) {
                case DM_ERROR_CRC: state = "CRC ERROR"; break;
                case DM_ERROR_WEAK: state = "SIGNAL LOST"; break;
                case DM_ERROR_HEADER: state = "HEADER ERROR"; break;
                case DM_ERROR_GAP: state = "SAMPLE LOSS"; break;
                }
            }
            oled_clear();
            {
                char title[24]; uint32_t k = 0;
#if AEGIS_ENABLE_ML
                const char *p = "AEGIS ML ";
#else
                const char *p = "AEGIS RAW ";
#endif
                while (*p) title[k++] = *p++;
                utoa_s(sysclk / 1000000u, b); for (p = b; *p; ) title[k++] = *p++;
                title[k++] = 'M'; title[k++] = ' ';
                { const char *c = clock_hse ? "XTAL" : "RC"; while (*c) title[k++] = *c++; }
                title[k] = 0;
                oled_text(0, 0, title);
            }
            oled_text(1, 0, state);
            if (dm.locked && dm.expected_bits) {
                oled_text(1, 8, utoa_s(100u * dm.bitcount / dm.expected_bits, b));
                oled_text(1, 11, "%");
            }
            oled_text(2, 0, "F0 "); oled_text(2, 3, utoa_s((uint32_t)(dm.amp0 + 0.5f), b));
            oled_text(2, 10, "F1 "); oled_text(2, 13, utoa_s((uint32_t)(dm.amp1 + 0.5f), b));
            oled_bar((uint32_t)(strong * 126.0f / 60.0f));
            memset(&fb[4u * 128u], 0, 128u);
#if AEGIS_ENABLE_ML
            oled_text(4, 0, receiver_ml_slow(sysclk) ? "ML SLOW" : "ML ON");
            oled_text(4, 9, utoa_s(receiver_peak_us(sysclk), b));
            oled_text(4, 16, "US");
#else
            oled_text(4, 0, "ML BYPASS");
#endif
            oled_text(5, 0, last_mode ? "AES:" : "RX: ");
            oled_text(5, 4, last[0] ? last + text_offset : "-");
            oled_text(6, 0, "OK "); oled_text(6, 3, utoa_s(dm.frames_ok, b));
            oled_text(6, 9, "BAD "); oled_text(6, 13, utoa_s(dm.frames_bad, b));
            oled_text(7, 0, "DC ");  oled_text(7, 3, utoa_s(s_cnt ? s_sum / s_cnt : 0u, b));
            oled_text(7, 8, "PP ");  oled_text(7, 11, utoa_s(s_cnt ? (uint32_t)(s_max - s_min) : 0u, b));
            oled_text(7, 16, "DR "); oled_text(7, 19, utoa_s(dropped + adc_overruns, b));
            s_min = 4095; s_max = 0; s_sum = 0; s_cnt = 0;
            oled_chunk = 0; oled_sending = 1;
        }
        {
            static uint32_t t_hb;
            if ((ms - t_hb) >= 2000u) {
                int sent = receiver_report_status(sysclk, dropped + adc_overruns);
                if (sent) t_hb = ms;
            }
        }
        if ((head - tail) < DM_HOP * 4u) oled_service();
        bt_service();
    }
}

#endif /* !DM_HOST_TEST */
