/* stream_probe: stock vs streaming openWakeWord embedding on the device.
 *
 *   stream_probe LIB STOCK.onnx STREAM.onnx [seconds] [xnnpack 0|1]
 *
 * Phase 1 feeds the same mel frames to both and compares the embeddings.
 * Phase 2 runs each at the real duty cycle (one call per 80 ms) and reports
 * process CPU, in the lines device/tools/oww_probe prints.
 * Session options are the firmware's: 1 thread, ORT_ENABLE_ALL, no spinning.
 */
#include <dlfcn.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include "onnxruntime_c_api.h"

#define NSTATE 11
#define STEP 8
#define WIN 76
#define MELS 32
#define EMB 96

static const OrtApi *api;
static void die(const char *what, OrtStatus *st) {
	fprintf(stderr, "FAIL %s: %s\n", what, st ? api->GetErrorMessage(st) : "");
	exit(1);
}
#define CK(what, call) do { OrtStatus *s_ = (call); if (s_) die(what, s_); } while (0)

static double now_ms(void) {
	struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}
static double cpu_s(void) {
	struct rusage r; getrusage(RUSAGE_SELF, &r);
	return r.ru_utime.tv_sec + r.ru_utime.tv_usec / 1e6 + r.ru_stime.tv_sec + r.ru_stime.tv_usec / 1e6;
}
static uint64_t seed = 0x9E3779B97F4A7C15ULL;
static float rnd(void) {            /* uint64_t: unsigned long is 32-bit here */
	seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
	return (float)((seed >> 33) % 2000000) / 1000000.0f * 6.0f - 6.0f;
}

static OrtSession *open_model(OrtEnv *env, const char *path, int xnn, int *xnn_on) {
	OrtSessionOptions *o; CK("opts", api->CreateSessionOptions(&o));
	CK("threads", api->SetIntraOpNumThreads(o, 1));
	CK("opt", api->SetSessionGraphOptimizationLevel(o, ORT_ENABLE_ALL));
	CK("spin", api->AddSessionConfigEntry(o, "session.intra_op.allow_spinning", "0"));
	CK("spin", api->AddSessionConfigEntry(o, "session.inter_op.allow_spinning", "0"));
	*xnn_on = 0;
	if (xnn) {
		const char *k[] = {"intra_op_num_threads"}, *v[] = {"1"};
		OrtStatus *st = api->SessionOptionsAppendExecutionProvider(o, "XNNPACK", k, v, 1);
		if (st) api->ReleaseStatus(st); else *xnn_on = 1;
	}
	OrtSession *s; CK(path, api->CreateSession(env, path, o, &s));
	api->ReleaseSessionOptions(o);
	return s;
}

static OrtMemoryInfo *mi;
static OrtValue *tensor(float *buf, size_t n, const int64_t *shape, size_t nd) {
	OrtValue *v; CK("tensor", api->CreateTensorWithDataAsOrtValue(mi, buf, n * sizeof(float), shape, nd,
	                                                             ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &v));
	return v;
}

/* stock: one 76-frame window in, one embedding out */
static void run_stock(OrtSession *s, float *win, float *emb) {
	int64_t sh[4] = {1, WIN, MELS, 1};
	OrtValue *in = tensor(win, WIN * MELS, sh, 4), *out = NULL;
	const char *inn[] = {"input_1"}, *outn[] = {"conv2d_19"};
	CK("run stock", api->Run(s, NULL, inn, (const OrtValue *const *)&in, 1, outn, 1, &out));
	float *p; CK("data", api->GetTensorMutableData(out, (void **)&p));
	memcpy(emb, p, EMB * sizeof(float));
	api->ReleaseValue(in); api->ReleaseValue(out);
}

/* streaming: 8 new frames and the states in, embedding and next states out */
static char in_names[NSTATE + 1][24], out_names[NSTATE + 1][24];
static const char *inp[NSTATE + 1], *outp[NSTATE + 1];
static float *state[NSTATE]; static size_t state_n[NSTATE]; static int64_t state_shape[NSTATE][4];

static void stream_init(OrtSession *s) {
	OrtAllocator *al; CK("alloc", api->GetAllocatorWithDefaultOptions(&al));
	size_t n; CK("count", api->SessionGetInputCount(s, &n));
	if (n != NSTATE + 1) { fprintf(stderr, "FAIL streaming model has %zu inputs, want %d\n", n, NSTATE + 1); exit(1); }
	strcpy(in_names[0], "mel"); strcpy(out_names[0], "embedding");
	for (int k = 0; k < NSTATE; k++) {
		snprintf(in_names[k + 1], 24, "state_%d", k);
		snprintf(out_names[k + 1], 24, "state_%d_next", k);
	}
	for (size_t i = 0; i < n; i++) {
		char *name; CK("name", api->SessionGetInputName(s, i, al, &name));
		int k = -1;
		if (sscanf(name, "state_%d", &k) == 1 && k >= 0 && k < NSTATE) {
			OrtTypeInfo *ti; CK("ti", api->SessionGetInputTypeInfo(s, i, &ti));
			const OrtTensorTypeAndShapeInfo *tsi; CK("cast", api->CastTypeInfoToTensorInfo(ti, &tsi));
			CK("dims", api->GetDimensions(tsi, state_shape[k], 4));
			state_n[k] = (size_t)(state_shape[k][0] * state_shape[k][1] * state_shape[k][2] * state_shape[k][3]);
			state[k] = calloc(state_n[k], sizeof(float));
			api->ReleaseTypeInfo(ti);
		}
		al->Free(al, name);
	}
	for (int i = 0; i <= NSTATE; i++) { inp[i] = in_names[i]; outp[i] = out_names[i]; }
}
static void stream_reset(void) { for (int k = 0; k < NSTATE; k++) memset(state[k], 0, state_n[k] * sizeof(float)); }

static void run_stream(OrtSession *s, float *mel8, float *emb) {
	int64_t sh[4] = {1, STEP, MELS, 1};
	OrtValue *in[NSTATE + 1], *out[NSTATE + 1];
	in[0] = tensor(mel8, STEP * MELS, sh, 4);
	for (int k = 0; k < NSTATE; k++) in[k + 1] = tensor(state[k], state_n[k], state_shape[k], 4);
	memset(out, 0, sizeof(out));
	CK("run stream", api->Run(s, NULL, inp, (const OrtValue *const *)in, NSTATE + 1, outp, NSTATE + 1, out));
	float *p; CK("data", api->GetTensorMutableData(out[0], (void **)&p));
	memcpy(emb, p, EMB * sizeof(float));
	for (int k = 0; k < NSTATE; k++) {
		CK("state", api->GetTensorMutableData(out[k + 1], (void **)&p));
		memcpy(state[k], p, state_n[k] * sizeof(float));
	}
	for (int i = 0; i <= NSTATE; i++) { api->ReleaseValue(in[i]); api->ReleaseValue(out[i]); }
}

static int cmp(const void *a, const void *b) { double d = *(const double *)a - *(const double *)b; return (d > 0) - (d < 0); }

typedef void (*step_fn)(OrtSession *, float *, float *);
static void paced(const char *label, OrtSession *s, step_fn fn, float *buf, int seconds) {
	int frames = seconds * 1000 / 80, late = 0;
	double *lat = malloc(frames * sizeof(double));
	float emb[EMB];
	double c0 = cpu_s(), t0 = now_ms();
	for (int i = 0; i < frames; i++) {
		double due = t0 + i * 80.0, a = now_ms();
		fn(s, buf, emb);
		lat[i] = now_ms() - a;
		if (lat[i] > 80.0) late++;
		double left = due + 80.0 - now_ms();
		if (left > 0) { struct timespec ts = {(time_t)(left / 1000), (long)(fmod(left, 1000) * 1e6)}; nanosleep(&ts, NULL); }
	}
	double wall = (now_ms() - t0) / 1000, cpu = cpu_s() - c0;
	qsort(lat, frames, sizeof(double), cmp);
	printf("\n== %s: cost at the real 12.5Hz duty cycle, %ds ==\n", label, seconds);
	printf("  frames     %d over %.1fs (%d overran their 80ms slot)\n", frames, wall, late);
	printf("  latency    p50 %.1fms  p95 %.1fms  max %.1fms  (budget 80ms)\n",
	       lat[frames / 2], lat[frames * 95 / 100], lat[frames - 1]);
	printf("  CPU        %.1f%% of one core  (%.2fs of CPU in %.1fs wall)\n", 100 * cpu / wall, cpu, wall);
	struct rusage r; getrusage(RUSAGE_SELF, &r);
	printf("  RSS        %ld KB peak\n", r.ru_maxrss);
	free(lat);
}

int main(int argc, char **argv) {
	if (argc < 4) { fprintf(stderr, "usage: stream_probe LIB STOCK.onnx STREAM.onnx [seconds] [xnnpack 0|1]\n"); return 2; }
	int seconds = argc > 4 ? atoi(argv[4]) : 30, xnn = argc > 5 ? atoi(argv[5]) : 1;
	void *dl = dlopen(argv[1], RTLD_NOW | RTLD_GLOBAL);
	if (!dl) { fprintf(stderr, "FAIL dlopen: %s\n", dlerror()); return 1; }
	const OrtApiBase *(*base)(void) = (const OrtApiBase *(*)(void))dlsym(dl, "OrtGetApiBase");
	api = base()->GetApi(ORT_API_VERSION);
	if (!api) { fprintf(stderr, "FAIL runtime too old: %s\n", base()->GetVersionString()); return 1; }
	OrtEnv *env; CK("env", api->CreateEnv(ORT_LOGGING_LEVEL_ERROR, "probe", &env));
	CK("mi", api->CreateCpuMemoryInfo(OrtArenaAllocator, OrtMemTypeDefault, &mi));
	int x0, x1;
	OrtSession *stock = open_model(env, argv[2], xnn, &x0), *stream = open_model(env, argv[3], xnn, &x1);
	stream_init(stream);
	printf("runtime   onnxruntime %s\nsession   threads=1 xnnpack=%d/%d(requested %d) spinning=0\n",
	       base()->GetVersionString(), x0, x1, xnn);

	/* phase 1: same frames to both */
	enum { STEPS = 60 };
	static float mel[STEPS * STEP * MELS];
	for (size_t i = 0; i < sizeof(mel) / sizeof(mel[0]); i++) mel[i] = rnd();
	float a[EMB], b[EMB]; double worst = 0, scale = 0; int compared = 0;
	stream_reset();
	for (int j = 0; j < STEPS; j++) {
		run_stream(stream, mel + j * STEP * MELS, b);
		int end = (j + 1) * STEP;
		if (end < WIN) continue;
		run_stock(stock, mel + (end - WIN) * MELS, a);
		for (int k = 0; k < EMB; k++) {
			double d = fabs((double)a[k] - b[k]); if (d > worst) worst = d;
			if (fabs(a[k]) > scale) scale = fabs(a[k]);
		}
		compared++;
	}
	printf("\n== phase 1: does streaming reproduce the stock model? ==\n");
	printf("  compared   %d steps after warm-up: max abs diff %.3g (values up to %.1f)\n", compared, worst, scale);
	printf("  VERDICT    %s\n", worst <= 1e-3 * (scale > 1 ? scale : 1) ? "pass" : "FAIL");

	paced("stock embedding", stock, run_stock, mel, seconds);
	stream_reset();
	paced("streaming embedding", stream, run_stream, mel, seconds);
	return 0;
}
