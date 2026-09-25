/* tools/flops.c - FMA throughput (AVX-512 or AVX2) with N threads for T seconds.
 * Build (both variants):
 *   gcc -O3 -march=native -pthread -o tools/flops      tools/flops.c
 *   gcc -O3 -march=native -mavx2 -mno-avx512f -pthread -o tools/flops_avx2 tools/flops.c
 * Answers "is it the clock?": the clock is a consequence of how many threads pull on
 * the package, and Tiger Lake can drop the clock on AVX-512. Together with
 * tools/sweep_threads.py (watts from energy_uj + effective clock from APERF/MPERF).
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <pthread.h>
#include <time.h>
#include <immintrin.h>

#if defined(__AVX512F__)
  #define LANES 16
  typedef __m512 V;
  static inline V vset(float x) { return _mm512_set1_ps(x); }
  static inline V vfma(V a, V b, V c) { return _mm512_fmadd_ps(a, b, c); }
  static inline V vadd(V a, V b) { return _mm512_add_ps(a, b); }
  #define SIMD "avx512"
#else
  #define LANES 8
  typedef __m256 V;
  static inline V vset(float x) { return _mm256_set1_ps(x); }
  static inline V vfma(V a, V b, V c) { return _mm256_fmadd_ps(a, b, c); }
  static inline V vadd(V a, V b) { return _mm256_add_ps(a, b); }
  #define SIMD "avx2"
#endif

static int NT = 1;
static double SECS = 25.0;
static unsigned long long iters[64];
static volatile float g_sink;


static void *worker(void *arg) {
    long id = (long)arg;
    V a[8];
    for (int c = 0; c < 8; c++) a[c] = vset(1.0f + 0.1f * (float)c + 0.001f * (float)id);
    /* contractive map: a = a*k + m converges to 1.0 (without blowing up to inf in 25 s) */
    const V k = vset(0.9999999f), m = vset(1e-7f);
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    unsigned long long n = 0;
    for (;;) {
        for (int u = 0; u < 256; u++) {
            #pragma GCC unroll 8
            for (int c = 0; c < 8; c++) a[c] = vfma(a[c], k, m);
        }
        n += 256;
        if ((n & 4095) == 0) {
            clock_gettime(CLOCK_MONOTONIC, &t1);
            double el = (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
            if (el >= SECS) break;
        }
    }
    V s = vadd(vadd(vadd(a[0], a[1]), vadd(a[2], a[3])),
               vadd(vadd(a[4], a[5]), vadd(a[6], a[7])));
    /* volatile + read in main: without that, -O3 drops the FMAs (dead stores) */
    static volatile float sink[LANES];
    float tmp[LANES];
    #if LANES == 16
      _mm512_storeu_ps(tmp, s);
    #else
      _mm256_storeu_ps(tmp, s);
    #endif
    for (int c = 0; c < LANES; c++) sink[c] = tmp[c];
    g_sink = sink[0] + tmp[LANES - 1];
    iters[id] = n;
    return NULL;
}

int main(int argc, char **argv) {
    if (argc > 1) NT = atoi(argv[1]);
    if (argc > 2) SECS = atof(argv[2]);
    if (NT < 1) NT = 1;
    if (NT > 64) NT = 64;
    pthread_t th[64];
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    for (long i = 0; i < NT; i++) pthread_create(&th[i], NULL, worker, (void *)i);
    for (int i = 0; i < NT; i++) pthread_join(th[i], NULL);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    double el = (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
    unsigned long long tot = 0;
    for (int i = 0; i < NT; i++) tot += iters[i];
    double gf = (double)tot * 8.0 * (double)LANES * 2.0 / el / 1e9;
    printf("%s threads=%d  %.1fs  %8.1f GFLOPS  (%6.1f /thread)  [sink %.3f]\n",
           SIMD, NT, el, gf, gf / NT, (double)g_sink);
    return 0;
}
