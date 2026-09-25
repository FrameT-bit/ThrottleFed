/*
 * throttlefed-helper - privileged sysfs writes for ThrottleFed.
 *
 * Why it exists in C: the GUI app runs as the user. It builds the PLAN (the
 * policy: which profile, which values) and this binary only EXECUTES writes
 * against a closed set of files (whitelist), returning what was written and
 * the value read back from sysfs (readback) - with no readback, "accepted"
 * is a guess.
 *
 * No external dependencies: gcc + libc. Neither gtk-devel nor glib is needed.
 *
 * Usage:
 *   throttlefed-helper apply    < plan.tsv      # lines: <target>\t<value>
 *   throttlefed-helper capture  [--force]       # saves the stock (root-only)
 *   throttlefed-helper restore                  # puts the saved stock back
 *   throttlefed-helper show                     # prints the saved stock
 *   throttlefed-helper sample   --seconds N [--interval MS]
 *   throttlefed-helper paths                    # paths found on this machine
 *
 * Targets accepted in the plan:
 *   /sys/...             real path, only if it matches the whitelist
 *   @epp                 value applied to ALL cpufreq policies
 *   @platform_profile    /sys/firmware/acpi/platform_profile
 *   @pp0                 PP0 (core) subzone: writes the limit and enables it
 *
 * Output: JSON on stdout. Exit 0 = every write confirmed by readback.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define STATE_DIR        "/var/lib/throttlefed"
#define STOCK_PATH       STATE_DIR "/stock.conf"
#define ACTIVE_PATH      STATE_DIR "/active.txt"
#define PLATFORM_PROFILE "/sys/firmware/acpi/platform_profile"

#define WHITELIST_MAX   128   /* discovered prefixes/exact files: a hard ceiling */
#define MAX_PLAN_ITEMS  4096  /* a bigger plan is refused whole, never truncated */
#define MAX_PATH        512
#define MAX_VAL         128
#define MAX_NOTE        24
#define NOTE_LEN        200
#define PL_LIMIT_MAX_UW 1000000000L  /* 1000 W: above this a stock is broken, not a limit */

/* Copy/concatenation with EXPLICIT truncation: the destination has a known
   size and the precision states the limit, so no byte goes past the buffer.
   (Without this gcc cannot prove the bound and floods -Wformat-truncation.) */
#define SETSTR(dst, src) \
    snprintf((dst), sizeof(dst), "%.*s", (int)(sizeof(dst) - 1), (const char *)(src))
#define SETSTRN(dst, cap, src) \
    snprintf((dst), (cap), "%.*s", (int)((cap) - 1), (const char *)(src))
#define APPEND(dst, src) \
    SETSTRN((dst) + strlen(dst), sizeof(dst) - strlen(dst), (src))
#define JOIN(dst, a, b) do { SETSTR((dst), (a)); APPEND((dst), (b)); } while (0)
#define JOINN(dst, cap, a, b) do { \
    SETSTRN((dst), (cap), (a)); \
    SETSTRN((dst) + strlen(dst), (cap) - strlen(dst), (b)); } while (0)

/* ------------------------------------------------------------------ */
/* notes: what this machine does NOT have, and what could not be read   */
/*                                                                     */
/* Every read that fails is reported here instead of being turned into  */
/* a plausible-looking default: the JSON of the command carries these   */
/* two lists, so a missing sensor/zone can never be mistaken for a      */
/* measured value.                                                      */
/* ------------------------------------------------------------------ */
static char warnings[MAX_NOTE][NOTE_LEN];
static int warn_n = 0;
static char missing[MAX_NOTE][NOTE_LEN];
static int missing_n = 0;

static void note_add(char (*arr)[NOTE_LEN], int *cnt, const char *fmt, ...) {
    if (*cnt >= MAX_NOTE) return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(arr[*cnt], NOTE_LEN, fmt, ap);
    va_end(ap);
    (*cnt)++;
}
#define warnf(...)       note_add(warnings, &warn_n, __VA_ARGS__)
#define missing_add(...) note_add(missing, &missing_n, __VA_ARGS__)

/* ------------------------------------------------------------------ */
/* whitelist: built at RUNTIME from what this machine actually exposes  */
/*                                                                     */
/* Nothing is writable that discovery did not find here: the RAPL       */
/* package zone (chosen by its 'name' field, never by index), the       */
/* subzone named 'core' (PP0) under that same parent, the cpufreq        */
/* policies that exist, the iGPU GT directory and platform_profile when */
/* the firmware publishes it. A fixed list of prefixes only encodes the */
/* enumeration of the machine it was written on.                        */
/* ------------------------------------------------------------------ */
static char wl_prefix[WHITELIST_MAX][MAX_PATH]; /* directories, prefix match */
static int wl_prefix_n = 0;
static char wl_exact[WHITELIST_MAX][MAX_PATH];  /* single files, exact match */
static int wl_exact_n = 0;

static int has_prefix(const char *s, const char *p) {
    return strncmp(s, p, strlen(p)) == 0;
}

static void wl_add_prefix(const char *dir) {
    if (!dir || dir[0] != '/' || strstr(dir, "..")) return;
    if (strlen(dir) + 2 > MAX_PATH) return;
    if (wl_prefix_n >= WHITELIST_MAX) {
        warnf("whitelist full (%d directories): %s will not be writable", WHITELIST_MAX, dir);
        return;
    }
    SETSTR(wl_prefix[wl_prefix_n], dir);
    APPEND(wl_prefix[wl_prefix_n], "/");
    wl_prefix_n++;
}

static void wl_add_exact(const char *file) {
    if (!file || file[0] != '/' || strstr(file, "..")) return;
    if (strlen(file) >= MAX_PATH) return;
    if (wl_exact_n >= WHITELIST_MAX) {
        warnf("whitelist full (%d files): %s will not be writable", WHITELIST_MAX, file);
        return;
    }
    SETSTR(wl_exact[wl_exact_n++], file);
}

static int path_allowed(const char *p) {
    if (strstr(p, "..") != NULL) return 0;
    if (p[0] != '/' || strlen(p) >= MAX_PATH) return 0;
    for (int i = 0; i < wl_exact_n; i++)
        if (strcmp(p, wl_exact[i]) == 0) return 1;
    for (int i = 0; i < wl_prefix_n; i++)
        if (has_prefix(p, wl_prefix[i])) return 1;
    return 0;
}

/* value: a word (a-z0-9_-) or an integer. no space, slash, newline. */
static int value_allowed(const char *v) {
    size_t n = strlen(v);
    if (n == 0 || n >= MAX_VAL) return 0;
    for (size_t i = 0; i < n; i++) {
        char c = v[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-'))
            return 0;
    }
    return 1;
}

/* ------------------------------------------------------------------ */
/* io helpers                                                          */
/* ------------------------------------------------------------------ */
static int read_text(const char *path, char *buf, size_t n) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;                 /* errno from open() is left intact */
    ssize_t r = read(fd, buf, n - 1);
    int e = errno;
    close(fd);
    if (r < 0) { errno = e; return -1; }
    if (r == 0) { errno = EIO; return -1; }  /* empty file: nothing was read */
    buf[r] = 0;
    while (r > 0 && (buf[r - 1] == '\n' || buf[r - 1] == ' ')) buf[--r] = 0;
    return 0;
}

/* Reads an integer from `path`.
   Returns 0 and stores the value when the file CAN be read and holds a number;
   returns errno when it cannot (absent, no permission, invalid content).
   A number that was not read is never the same as a zero that was read: the
   caller is forced to decide what to do with the failure. */
static int read_long_ok(const char *path, long *out) {
    char b[64];
    if (read_text(path, b, sizeof b) != 0) return errno ? errno : EIO;
    char *end = NULL;
    errno = 0;
    long v = strtol(b, &end, 10);
    if (end == b || errno == ERANGE) return EINVAL;  /* empty or not a number */
    *out = v;
    return 0;
}

static const char *read_err_text(int e) {
    return e == EINVAL ? "content is not a number" : strerror(e);
}

static int write_text(const char *path, const char *val) {
    int fd = open(path, O_WRONLY);
    if (fd < 0) return -errno;
    size_t n = strlen(val);
    ssize_t w = write(fd, val, n);
    int e = errno;
    close(fd);
    if (w != (ssize_t)n) return -(e ? e : EIO);
    return 0;
}

static void json_escape(const char *s, char *out, size_t n) {
    size_t j = 0;
    for (size_t i = 0; s[i] && j + 7 < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == '"' || c == '\\') { out[j++] = '\\'; out[j++] = (char)c; }
        else if (c < 0x20) j += snprintf(out + j, n - j, "\\u%04x", c);
        else out[j++] = (char)c;
    }
    out[j] = 0;
}

static void json_str(const char *s) {
    char esc[MAX_PATH * 2];
    json_escape(s ? s : "", esc, sizeof esc);
    printf("\"%s\"", esc);
}

static void notes_json(const char *key, char (*arr)[NOTE_LEN], int n) {
    printf(",\"%s\":[", key);
    for (int i = 0; i < n; i++) { if (i) printf(","); json_str(arr[i]); }
    printf("]");
}

static void print_notes(void) {
    notes_json("missing", missing, missing_n);
    notes_json("warnings", warnings, warn_n);
}

static const char *errname(int e) {
    int a = e < 0 ? -e : e;
    switch (a) {
    case EPERM:  return "EPERM (locked by the firmware/kernel)";
    case EACCES: return "EACCES (no permission)";
    case EINVAL: return "EINVAL (value refused by the driver)";
    case ENOENT: return "ENOENT (does not exist)";
    case EROFS:  return "EROFS (read-only)";
    case 0:      return "ok";
    default:     return strerror(a);
    }
}

/* ------------------------------------------------------------------ */
/* path discovery                                                     */
/* ------------------------------------------------------------------ */
static char pkg_dirs[8][MAX_PATH];  /* every RAPL zone named 'package-*' */
static int pkg_n = 0;
static char pkg_dir[MAX_PATH] = "";      /* preferred package zone (MSR tree first) */
static char core_dir[MAX_PATH] = "";     /* subzone named 'core' (PP0), same parent */
static char platform_path[MAX_PATH] = "";/* only when the firmware publishes it */
static char gt_dir[MAX_PATH] = "";       /* iGPU GT directory, when found */
static char gt_min_path[MAX_PATH] = "";  /* <gt_dir>/rps_min_freq_mhz */
static char **epp_paths = NULL;          /* every cpufreq policy that has EPP */
static int epp_n = 0;
static int have_cpufreq = 0;

/* reads the 'name' field of a powercap zone */
static int zone_name(const char *dir, char *out, size_t n) {
    char p[MAX_PATH];
    JOIN(p, dir, "/name");
    return read_text(p, out, n);
}

/* The package zone is chosen by CONTENT: any intel-rapl* whose 'name' is
   'package-*'. The MSR tree (intel-rapl:N) beats the MMIO one
   (intel-rapl-mmio:N) when both exist, because MMIO usually does not expose
   subzones. */
static void discover_package(void) {
    glob_t g;
    if (glob("/sys/class/powercap/intel-rapl*", 0, NULL, &g) != 0) return;
    char msr[MAX_PATH] = "", mmio[MAX_PATH] = "";
    for (size_t i = 0; i < g.gl_pathc; i++) {
        const char *p = g.gl_pathv[i];
        char nm[MAX_PATH];
        if (zone_name(p, nm, sizeof nm) != 0) continue;  /* class directory: no 'name' */
        if (strncmp(nm, "package-", 8) != 0) continue;   /* 'core', 'uncore', 'psys' */
        if (pkg_n < (int)(sizeof pkg_dirs / sizeof pkg_dirs[0]))
            SETSTR(pkg_dirs[pkg_n++], p);
        if (strstr(p, "-mmio:")) { if (!mmio[0]) SETSTR(mmio, p); }
        else if (!msr[0]) SETSTR(msr, p);
        wl_add_prefix(p);
    }
    globfree(&g);
    SETSTR(pkg_dir, msr[0] ? msr : mmio);
    if (pkg_n == 0)
        missing_add("RAPL: no /sys/class/powercap/intel-rapl* exposes a zone named 'package-*'");
}

/* 'core' subzone (PP0): walks the subzones of the SAME parent as the
   package zone */
static void discover_core(void) {
    for (int k = 0; k < pkg_n; k++) {
        char pat[MAX_PATH];
        JOIN(pat, pkg_dirs[k], ":*");
        glob_t g;
        if (glob(pat, 0, NULL, &g) != 0) continue;
        for (size_t i = 0; i < g.gl_pathc; i++) {
            char nm[MAX_PATH];
            if (zone_name(g.gl_pathv[i], nm, sizeof nm) != 0) continue;
            if (strcmp(nm, "core") != 0) continue;
            if (!core_dir[0]) SETSTR(core_dir, g.gl_pathv[i]);
            wl_add_prefix(g.gl_pathv[i]);
        }
        globfree(&g);
    }
    if (!core_dir[0])
        missing_add("RAPL PP0: none of the package subzones is named 'core'");
}

static void discover_platform(void) {
    if (access(PLATFORM_PROFILE, F_OK) == 0) {
        SETSTR(platform_path, PLATFORM_PROFILE);
        wl_add_exact(platform_path);
    } else {
        missing_add("platform_profile: %s does not exist on this firmware", PLATFORM_PROFILE);
    }
}

static void discover_cpufreq(void) {
    glob_t g;
    if (glob("/sys/devices/system/cpu/cpufreq/policy*", 0, NULL, &g) == 0) {
        for (size_t i = 0; i < g.gl_pathc; i++) wl_add_prefix(g.gl_pathv[i]);
        have_cpufreq = g.gl_pathc > 0;
        globfree(&g);
    }
    if (!have_cpufreq) {
        missing_add("cpufreq: no /sys/devices/system/cpu/cpufreq/policy* on this machine");
        return;
    }
    glob_t e;
    if (glob("/sys/devices/system/cpu/cpufreq/policy*/energy_performance_preference",
             0, NULL, &e) != 0) {
        warnf("no cpufreq policy publishes energy_performance_preference: EPP cannot be set");
        return;
    }
    /* dynamic: the number of policies depends on the machine, not a literal */
    epp_paths = malloc(sizeof(*epp_paths) * e.gl_pathc);
    if (!epp_paths) {
        warnf("out of memory listing %zu cpufreq policies: EPP cannot be set", e.gl_pathc);
        globfree(&e);
        return;
    }
    for (size_t i = 0; i < e.gl_pathc; i++) {
        char *s = strdup(e.gl_pathv[i]);
        if (!s) {
            warnf("out of memory storing %s: %zu of %zu policies kept",
                  e.gl_pathv[i], (size_t)epp_n, e.gl_pathc);
            break;
        }
        epp_paths[epp_n++] = s;
    }
    globfree(&e);
}

/* i915 exposes <card>/gt/gtN/...; the xe driver exposes
   <card>/device/tileN/gtN/... */
static int card_is_intel(const char *path) {
    static const char root[] = "/sys/class/drm/";
    char dir[MAX_PATH], vend[MAX_PATH], buf[64];
    SETSTR(dir, path);
    if (!has_prefix(dir, root)) return 0;
    char *slash = strchr(dir + sizeof(root) - 1, '/');
    if (!slash) return 0;
    *slash = 0;
    JOIN(vend, dir, "/device/vendor");
    if (read_text(vend, buf, sizeof buf) != 0) return 0;
    return strncmp(buf, "0x8086", 6) == 0 || strncmp(buf, "8086", 4) == 0;
}

static void discover_gt(void) {
    static const char *pats[] = {
        "/sys/class/drm/card[0-9]*/gt/gt[0-9]*/rps_min_freq_mhz",
        "/sys/class/drm/card[0-9]*/device/tile[0-9]*/gt[0-9]*/rps_min_freq_mhz",
    };
    char first[MAX_PATH] = "", intel[MAX_PATH] = "";
    for (size_t k = 0; k < sizeof pats / sizeof pats[0]; k++) {
        glob_t g;
        if (glob(pats[k], 0, NULL, &g) != 0) continue;
        for (size_t i = 0; i < g.gl_pathc; i++) {
            if (!first[0]) SETSTR(first, g.gl_pathv[i]);
            /* on a machine with a dGPU, do not cap the frequency of the
               wrong device */
            if (!intel[0] && card_is_intel(g.gl_pathv[i])) SETSTR(intel, g.gl_pathv[i]);
        }
        globfree(&g);
    }
    SETSTR(gt_min_path, intel[0] ? intel : first);
    if (!gt_min_path[0]) {
        missing_add("iGPU GT: no /sys/class/drm/card*/gt/gt* (i915) or card*/device/tile*/gt* (xe)");
        return;
    }
    SETSTR(gt_dir, gt_min_path);
    char *slash = strrchr(gt_dir, '/');
    if (slash) {
        *slash = 0;
        wl_add_prefix(gt_dir);
    }
}

static void discover(void) {
    discover_package();
    discover_core();
    discover_platform();
    discover_cpufreq();
    discover_gt();
}

/* ------------------------------------------------------------------ */
/* stock                                                               */
/* ------------------------------------------------------------------ */
typedef struct {
    long pl1_uw, pl2_uw, pp0_uw;
    long pl2_win_us;   /* PL2 window (burst) in us - 0 = not captured */
    int pp0_en;
    char epp[32], thermal[32];
    long gt_min;       /* 0 = not captured (0 MHz is not a valid frequency) */
} stock_t;

static int load_stock(stock_t *s) {
    FILE *f = fopen(STOCK_PATH, "r");
    if (!f) return -1;
    memset(s, 0, sizeof *s);
    char line[256];
    while (fgets(line, sizeof line, f)) {
        char k[64], v[64];
        if (sscanf(line, "%63[^=]=%63s", k, v) != 2) continue;
        if (!strcmp(k, "pl1_uw")) s->pl1_uw = strtol(v, NULL, 10);
        else if (!strcmp(k, "pl2_uw")) s->pl2_uw = strtol(v, NULL, 10);
        else if (!strcmp(k, "pp0_uw")) s->pp0_uw = strtol(v, NULL, 10);
        else if (!strcmp(k, "pp0_en")) s->pp0_en = atoi(v);
        else if (!strcmp(k, "epp")) SETSTR(s->epp, v);
        else if (!strcmp(k, "thermal")) SETSTR(s->thermal, v);
        else if (!strcmp(k, "gt_min")) s->gt_min = strtol(v, NULL, 10);
        else if (!strcmp(k, "pl2_win_us")) s->pl2_win_us = strtol(v, NULL, 10);
    }
    fclose(f);
    return 0;
}

/* mkdir(2) instead of system("mkdir -p"): it does not depend on /bin/sh, EEXIST
   is the normal case (the directory already existed) and any other failure is
   returned with the path and the errno, instead of turning into a generic
   fopen just below. */
static int ensure_dir(const char *path, char *why, size_t n) {
    if (mkdir(path, 0755) == 0) return 0;
    if (errno == EEXIST) return 0; /* it already existed: not a failure */
    snprintf(why, n, "could not create %s: %s", path, strerror(errno));
    return -1;
}

static int ensure_state_dir(char *why, size_t n) {
    return ensure_dir(STATE_DIR, why, n);
}

static int save_stock(const stock_t *s, char *why, size_t n) {
    if (ensure_state_dir(why, n) != 0) return -1;
    FILE *f = fopen(STOCK_PATH ".tmp", "w");
    if (!f) {
        snprintf(why, n, "could not write %s.tmp: %s", STOCK_PATH, strerror(errno));
        return -1;
    }
    fprintf(f, "pl1_uw=%ld\npl2_uw=%ld\npp0_uw=%ld\npp0_en=%d\n",
            s->pl1_uw, s->pl2_uw, s->pp0_uw, s->pp0_en);
    fprintf(f, "epp=%s\nthermal=%s\ngt_min=%ld\npl2_win_us=%ld\n",
            s->epp, s->thermal, s->gt_min, s->pl2_win_us);
    if (ferror(f)) {
        fclose(f);
        snprintf(why, n, "could not write %s.tmp: %s", STOCK_PATH, strerror(errno));
        return -1;
    }
    if (fclose(f) != 0) {
        snprintf(why, n, "could not flush %s.tmp: %s", STOCK_PATH, strerror(errno));
        return -1;
    }
    if (rename(STOCK_PATH ".tmp", STOCK_PATH) != 0) {
        snprintf(why, n, "could not move %s.tmp to %s: %s",
                 STOCK_PATH, STOCK_PATH, strerror(errno));
        return -1;
    }
    chmod(STOCK_PATH, 0644);
    return 0;
}

static void write_active(const char *name) {
    if (!name || !name[0] || !value_allowed(name)) return;
    char why[NOTE_LEN];
    if (ensure_state_dir(why, sizeof why) != 0) { warnf("%s", why); return; }
    FILE *f = fopen(ACTIVE_PATH, "w");
    if (!f) { warnf("could not write %s: %s", ACTIVE_PATH, strerror(errno)); return; }
    fprintf(f, "%s\n", name);
    if (fclose(f) != 0) { warnf("could not flush %s: %s", ACTIVE_PATH, strerror(errno)); return; }
    chmod(ACTIVE_PATH, 0644);
}

/* Reads the LIVE stock from sysfs. Returns 0 only when every read that the
   restore would later rewrite was actually made: a failed read never becomes
   0 here, because 0 W is a value that looks like a valid limit and would be
   written back. `why` names the read that was missing. */
static int read_stock_now(stock_t *s, char *why, size_t n) {
    memset(s, 0, sizeof *s);
    char p[MAX_PATH], buf[64];
    if (!pkg_dir[0]) {
        snprintf(why, n, "RAPL package zone not found: no /sys/class/powercap/intel-rapl* "
                         "exposes a zone named 'package-*'; nothing to capture");
        return -1;
    }
    JOIN(p, pkg_dir, "/constraint_0_power_limit_uw");
    int e = read_long_ok(p, &s->pl1_uw);
    if (e != 0) {
        snprintf(why, n, "could not read %s (%s); refusing to save a stock with a missing PL1",
                 p, read_err_text(e));
        return -1;
    }
    JOIN(p, pkg_dir, "/constraint_1_power_limit_uw");
    e = read_long_ok(p, &s->pl2_uw);
    if (e != 0) {
        snprintf(why, n, "could not read %s (%s); refusing to save a stock with a missing PL2",
                 p, read_err_text(e));
        return -1;
    }
    /* burst window: it is not a limit, and 0 keeps the old meaning ("not
       captured"); restore skips the key instead of writing 0 and killing the
       firmware burst. */
    JOIN(p, pkg_dir, "/constraint_1_time_window_us");
    long win = 0;
    if (read_long_ok(p, &win) == 0) s->pl2_win_us = win;
    else warnf("could not read %s: PL2 burst window not captured (restore will leave it alone)", p);

    if (core_dir[0]) {
        JOIN(p, core_dir, "/constraint_0_power_limit_uw");
        e = read_long_ok(p, &s->pp0_uw);
        if (e != 0) {
            snprintf(why, n, "could not read %s (%s); refusing to save a stock with a missing PP0",
                     p, read_err_text(e));
            return -1;
        }
        long en = 0;
        JOIN(p, core_dir, "/enabled");
        e = read_long_ok(p, &en);
        if (e != 0) {
            snprintf(why, n, "could not read %s (%s); refusing to save a stock with an unknown PP0 state",
                     p, read_err_text(e));
            return -1;
        }
        s->pp0_en = (int)en;
    } else {
        warnf("no RAPL subzone named 'core' (PP0): PP0 not captured");
    }

    if (epp_n > 0) {
        if (read_text(epp_paths[0], buf, sizeof buf) == 0) SETSTR(s->epp, buf);
        else warnf("could not read %s: EPP not captured (restore will leave it alone)", epp_paths[0]);
    } else {
        warnf("no cpufreq policy with energy_performance_preference: EPP not captured");
    }

    if (platform_path[0]) {
        if (read_text(platform_path, buf, sizeof buf) == 0) SETSTR(s->thermal, buf);
        else warnf("could not read %s: platform_profile not captured", platform_path);
    } else {
        warnf("this firmware does not publish %s: platform_profile not captured", PLATFORM_PROFILE);
    }

    if (gt_min_path[0]) {
        long g = 0;
        if (read_long_ok(gt_min_path, &g) == 0) s->gt_min = g;
        else warnf("could not read %s: GT min frequency not captured (restore will leave it alone)",
                   gt_min_path);
    } else {
        warnf("no iGPU GT directory found: GT min frequency not captured");
    }
    return 0;
}

/* ------------------------------------------------------------------ */
/* writing one target + readback                                      */
/* ------------------------------------------------------------------ */
typedef struct {
    char target[MAX_PATH];
    char real[MAX_PATH];
    char wrote[MAX_VAL];
    char readback[MAX_VAL];
    char reason[NOTE_LEN];  /* why this item cannot be written, in words */
    int ok;
    int err;
    int rejected; /* 1 = outside the whitelist, 2 = invalid value */
} item_t;

/* Why a real path is not writable, in words: whoever reads the JSON should
   never have to guess whether it was a '..', a typo or a domain that this
   machine simply does not have. */
static void target_reason(const char *t, char *why, size_t n) {
    if (strstr(t, "..") != NULL) {
        snprintf(why, n, "path contains '..'");
        return;
    }
    if (t[0] != '/') {
        snprintf(why, n, "target is not an absolute path");
        return;
    }
    if (strlen(t) >= MAX_PATH) {
        snprintf(why, n, "path is longer than %d bytes", MAX_PATH);
        return;
    }
    if (has_prefix(t, "/sys/class/powercap/") && pkg_n == 0) {
        snprintf(why, n, "RAPL not found: no /sys/class/powercap/intel-rapl* exposes "
                         "a zone named 'package-*' on this machine");
        return;
    }
    if (strstr(t, "/cpufreq/policy") != NULL && !have_cpufreq) {
        snprintf(why, n, "cpufreq not found: no /sys/devices/system/cpu/cpufreq/policy* "
                         "on this machine");
        return;
    }
    /* The discovered paths come out whole in 'paths' and in "whitelist":
       here it is enough to say WHICH domains this machine exposed, so the
       reason fits. */
    snprintf(why, n, "not in the discovered whitelist (RAPL %s, PP0 %s, %d cpufreq "
                     "policies, platform_profile %s, GT %s)",
             pkg_n ? "found" : "absent",
             core_dir[0] ? "found" : "absent",
             epp_n,
             platform_path[0] ? "found" : "absent",
             gt_min_path[0] ? "found" : "absent");
}

/* A DISCOVERY failure is not an item failure: if the plan asks for a domain
   that this machine does not expose at all, say ONCE which domain was missing
   and stop, instead of refusing item by item with no cause. */
static const char *plan_gap(const item_t *items, int n) {
    for (int i = 0; i < n; i++) {
        const char *t = items[i].target;
        if (!strcmp(t, "@pp0") && !core_dir[0])
            return "plan asks for PP0 (@pp0) but this machine has no RAPL subzone named 'core'";
        if (!strcmp(t, "@epp") && !have_cpufreq)
            return "plan asks for EPP (@epp) but this machine has no "
                   "/sys/devices/system/cpu/cpufreq/policy*";
        if (has_prefix(t, "/sys/class/powercap/") && pkg_n == 0)
            return "plan writes to /sys/class/powercap but no intel-rapl* zone named "
                   "'package-*' exists on this machine";
        if (strstr(t, "/cpufreq/policy") && !have_cpufreq)
            return "plan writes to cpufreq but no /sys/devices/system/cpu/cpufreq/policy* "
                   "exists on this machine";
    }
    return NULL;
}

/* ------------------------------------------------------------------ */
/* firmware target: @fwa:<vendor>:<attribute>                         */
/*                                                                    */
/* The path is BUILT here, it never comes ready-made from the caller,  */
/* so no traversal is possible. The name only accepts a letter, a      */
/* digit, _ and -, and the value has to be published by the firmware   */
/* in possible_values: what the hardware owner does not offer, this    */
/* helper does not write.                                              */
/* ------------------------------------------------------------------ */
static int read_text(const char *path, char *buf, size_t n); /* defined below */

static int fwa_token_ok(const char *s, size_t n) {
    if (n == 0 || n > 64) return 0;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-'))
            return 0;
    }
    return 1;
}

static int fwa_path(const char *target, char *out, size_t n) {
    const char *rest = target + 5; /* after "@fwa:" */
    const char *colon = strchr(rest, ':');
    if (!colon) return 0;
    size_t vlen = (size_t)(colon - rest);
    if (!fwa_token_ok(rest, vlen)) return 0;
    const char *name = colon + 1;
    if (!fwa_token_ok(name, strlen(name))) return 0;
    /* the vendor token ends at the ':' - copy it before building the path,
       otherwise the attribute name takes the vendor's place and nothing
       exists */
    char vendor[65];
    memcpy(vendor, rest, vlen);
    vendor[vlen] = '\0';
    char dir[MAX_PATH];
    int w = snprintf(dir, sizeof dir,
                     "/sys/class/firmware-attributes/%s/attributes/%s", vendor, name);
    if (w <= 0 || (size_t)w >= sizeof dir) return 0;
    char probe[MAX_PATH];
    JOINN(probe, MAX_PATH, dir, "/current_value");
    if (access(probe, F_OK) != 0) return 0;
    /* the returned path is current_value; possible_values sits next to it */
    SETSTRN(out, n, probe);
    return 1;
}

/* attribute value: accepts what the firmware published, and nothing else. */
static int fwa_value_allowed(const char *target, const char *v) {
    size_t n = strlen(v);
    if (n == 0 || n >= MAX_VAL) return 0;
    for (size_t i = 0; i < n; i++) {
        char c = v[i];
        if (c == '/' || c == ';' || c == '$' || c == '`' || c == '\\' ||
            c == '"' || c == '\'' || c == '<' || c == '>' || c == '|' ||
            c == '&' || c == '\n' || c == '\r' || c == '\t' || c < 0x20)
            return 0;
    }
    char file[MAX_PATH];
    if (!fwa_path(target, file, sizeof file)) return 0;
    char dir[MAX_PATH];
    SETSTR(dir, file);
    char *slash = strrchr(dir, '/');
    if (!slash) return 0;
    *slash = '\0';
    char possible[MAX_PATH];
    JOINN(possible, MAX_PATH, dir, "/possible_values");
    char buf[1024];
    /* read_text returns 0 on success: with no published list, the kernel decides */
    if (read_text(possible, buf, sizeof buf) != 0) return 1;
    const char *p = buf;
    while (*p) {
        const char *semi = strchr(p, ';');
        size_t len = semi ? (size_t)(semi - p) : strlen(p);
        if (len == n && strncmp(p, v, len) == 0) return 1;
        if (!semi) break;
        p = semi + 1;
    }
    return 0;
}

/* Writes ONE already validated item. The target and the value come from INSIDE
   the item itself (phase 1 already stored them): if they were passed as strings
   and the same buffer were reused, snprintf would erase the source before
   reading it. */
static int write_one(item_t *it, int dry) {
    const char *target = it->target;
    const char *val = it->wrote;
    it->readback[0] = 0;
    it->reason[0] = 0;
    it->ok = 0;
    it->err = 0;

    char buf0[MAX_PATH], buf1[MAX_PATH];
    const char *paths[2];
    int n = 0;
    int is_epp = 0;

    if (strcmp(target, "@epp") == 0) {
        if (epp_n == 0) {
            it->err = ENOENT;
            snprintf(it->reason, sizeof it->reason,
                     "no cpufreq policy with energy_performance_preference on this machine");
            return -1;
        }
        is_epp = 1;
        SETSTR(it->real, epp_paths[0]);
    } else if (strcmp(target, "@platform_profile") == 0) {
        if (!platform_path[0]) {
            it->err = ENOENT;
            snprintf(it->reason, sizeof it->reason,
                     "this firmware does not publish %s", PLATFORM_PROFILE);
            return -1;
        }
        SETSTR(buf0, platform_path);
        paths[n++] = buf0;
        SETSTR(it->real, platform_path);
    } else if (strcmp(target, "@pp0") == 0) {
        if (!core_dir[0]) {
            it->err = ENOENT;
            snprintf(it->reason, sizeof it->reason,
                     "no RAPL subzone named 'core' (PP0) under %.120s",
                     pkg_dir[0] ? pkg_dir : "/sys/class/powercap");
            return -1;
        }
        JOIN(buf0, core_dir, "/constraint_0_power_limit_uw");
        JOIN(buf1, core_dir, "/enabled");
        paths[n++] = buf0;
        paths[n++] = buf1;
        SETSTR(it->real, buf0);
    } else if (strncmp(target, "@fwa:", 5) == 0) {
        if (!fwa_path(target, buf0, MAX_PATH)) {
            it->err = EPERM;
            snprintf(it->reason, sizeof it->reason,
                     "@fwa target is not a firmware attribute on this machine (it needs "
                     "/sys/class/firmware-attributes/<vendor>/attributes/<name>/current_value)");
            return -1;
        }
        paths[n++] = buf0;
        SETSTR(it->real, buf0);
    } else {
        if (!path_allowed(target)) {
            it->err = EPERM;
            target_reason(target, it->reason, sizeof it->reason);
            return -1;
        }
        SETSTR(buf0, target);
        paths[n++] = buf0;
        SETSTR(it->real, target);
    }

    int failed = 0;
    if (is_epp) {
            /* every discovered policy, however many: the vector is dynamic */
        for (int i = 0; i < epp_n; i++) {
            if (dry) continue;
            int e = write_text(epp_paths[i], val);
            if (e != 0) { failed = e; break; }
        }
    } else {
        for (int i = 0; i < n; i++) {
            /* @pp0: limit first, enabled after (the kernel requires this
               order); value 0 = turn the subzone off instead of enabling it
               with a zero cap. */
            const char *v = val;
            if (dry) continue;
            if (strcmp(target, "@pp0") == 0 && i == 1)
                v = (strcmp(val, "0") == 0) ? "0" : "1";
            int e = write_text(paths[i], v);
            if (e != 0) { failed = e; break; }
        }
    }
    if (failed != 0) {
        it->err = failed;
        snprintf(it->reason, sizeof it->reason, "write failed: %s", errname(failed));
        return -1;
    }

    char rb[64];
    if (read_text(it->real, rb, sizeof rb) == 0)
        SETSTR(it->readback, rb);
    else
        warnf("wrote %s but could not read it back (%s): the readback is unknown",
              it->real, strerror(errno));
    it->ok = 1;
    return 0;
}

/* ------------------------------------------------------------------ */
/* sample: real package power from energy_uj                          */
/* ------------------------------------------------------------------ */
/* Really reads the wrap-around width of the counter. Without it, a negative
   delta can be a wrap or a counter reset: there is no way to tell the two
   apart, so the energy is not computed (no value hardcoded from this machine). */
static int max_energy_range(long *out, char *why, size_t n) {
    if (!pkg_dir[0]) {
        snprintf(why, n, "no RAPL package zone found: cannot read max_energy_range_uj");
        return -1;
    }
    char p[MAX_PATH];
    JOIN(p, pkg_dir, "/max_energy_range_uj");
    int e = read_long_ok(p, out);
    if (e != 0) {
        snprintf(why, n, "cannot read %s (%s): without the counter width a wrap-around "
                         "cannot be told apart from a counter reset", p, read_err_text(e));
        return -1;
    }
    return 0;
}

static int cmd_sample(long seconds, long interval_ms) {
    char p[MAX_PATH], why[NOTE_LEN];
    if (!pkg_dir[0]) {
        printf("{\"ok\":false,\"error\":\"no RAPL package zone found: "
               "no /sys/class/powercap/intel-rapl* exposes a zone named 'package-*'\"}\n");
        return 1;
    }
    JOIN(p, pkg_dir, "/energy_uj");
    long max_r = 0;
    int have_range = (max_energy_range(&max_r, why, sizeof why) == 0);
    long prev = 0;
    if (read_long_ok(p, &prev) != 0) {
        printf("{\"ok\":false,\"error\":\"could not read %s (energy_uj is root-only in the kernel)\"}\n", p);
        return 1;
    }
    if (!have_range) {
        printf("{\"ok\":false,\"reliable\":false,\"seconds\":%ld,\"interval_ms\":%ld,"
               "\"n\":0,\"avg_w\":null,\"max_w\":null,\"samples\":[],\"error\":",
               seconds, interval_ms);
        json_str(why);
        print_notes();
        printf("}\n");
        return 1;
    }
    double *ws = malloc(sizeof(double) * 4096);
    if (!ws) {
        printf("{\"ok\":false,\"error\":\"out of memory for the sample buffer\"}\n");
        return 1;
    }
    int n = 0;
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    long elapsed_target = seconds * 1000;
    long elapsed = 0;
    double sum = 0;
    while (elapsed < elapsed_target && n < 4096) {
        struct timespec ts = {interval_ms / 1000, (interval_ms % 1000) * 1000000L};
        nanosleep(&ts, NULL);
        long cur = 0;
        if (read_long_ok(p, &cur) != 0) {
            warnf("could not read %s mid-sampling (sample %d): stopped early", p, n + 1);
            break;
        }
        double d = (double)(cur - prev);
        if (d < 0) d += (double)max_r;           /* counter wrap */
        double w = d / ((double)interval_ms / 1000.0) / 1e6;  /* uJ -> W */
        ws[n++] = w;
        sum += w;
        prev = cur;
        clock_gettime(CLOCK_MONOTONIC, &t1);
        elapsed = (t1.tv_sec - t0.tv_sec) * 1000 + (t1.tv_nsec - t0.tv_nsec) / 1000000;
    }
    if (n == 0) {
        printf("{\"ok\":false,\"error\":\"no sample could be read from %s\"", p);
        print_notes();
        printf("}\n");
        free(ws);
        return 1;
    }
    if (n >= 4096) warnf("sample buffer full: stopped at %d samples before %ld ms", n, elapsed_target);
    double mx = 0;
    for (int i = 0; i < n; i++) if (ws[i] > mx) mx = ws[i];
    printf("{\"ok\":true,\"reliable\":true,\"seconds\":%ld,\"interval_ms\":%ld,\"n\":%d,"
           "\"energy_range_uj\":%ld,\"avg_w\":%.3f,\"max_w\":%.3f,\"samples\":[",
           seconds, interval_ms, n, max_r, sum / n, mx);
    for (int i = 0; i < n; i++) printf("%s%.3f", i ? "," : "", ws[i]);
    printf("]");
    print_notes();
    printf("}\n");
    free(ws);
    return 0;
}

/* ------------------------------------------------------------------ */
static void usage(void) {
    fprintf(stderr,
        "throttlefed-helper - privileged writes for ThrottleFed\n\n"
        "  apply   < plan.tsv    lines \"target<TAB>value\" (see header)\n"
        "  check   < plan.tsv    only validates the plan (whitelist), nothing is written\n"
        "  capture [--force]     saves the stock to " STOCK_PATH "\n"
        "  restore               re-applies the saved stock\n"
        "  show                  prints the saved stock\n"
        "  sample  --seconds N [--interval MS]\n"
        "  paths                 shows the discovered paths\n");
}

static void cmd_paths(void) {
    printf("{\"ok\":%s,\"pkg\":", pkg_n ? "true" : "false");
    json_str(pkg_dir);
    printf(",\"core\":");
    json_str(core_dir);
    printf(",\"platform_profile\":");
    json_str(platform_path);
    printf(",\"gt_min\":");
    json_str(gt_min_path);
    printf(",\"epp_paths\":[");
    for (int i = 0; i < epp_n; i++) { if (i) printf(","); json_str(epp_paths[i]); }
    printf("],\"epp_n\":%d", epp_n);
    printf(",\"pkg_zones\":[");
    for (int i = 0; i < pkg_n; i++) { if (i) printf(","); json_str(pkg_dirs[i]); }
    printf("],\"whitelist\":[");
    for (int i = 0; i < wl_prefix_n; i++) { if (i) printf(","); json_str(wl_prefix[i]); }
    printf("],\"whitelist_files\":[");
    for (int i = 0; i < wl_exact_n; i++) { if (i) printf(","); json_str(wl_exact[i]); }
    printf("]");
    print_notes();
    printf("}\n");
}

static void stock_json(const stock_t *s, const char *tag) {
    printf("{\"ok\":true,\"%s\":{\"pl1_uw\":%ld,\"pl2_uw\":%ld,\"pl2_win_us\":%ld,"
           "\"pp0_uw\":%ld,\"pp0_en\":%d,\"epp\":", tag, s->pl1_uw, s->pl2_uw,
           s->pl2_win_us, s->pp0_uw, s->pp0_en);
    json_str(s->epp);
    printf(",\"thermal\":");
    json_str(s->thermal);
    printf(",\"gt_min\":%ld}}\n", s->gt_min);
}

/* A stock is only good for being rewritten if every limit inside it is a real
   reading. A zeroed limit means "I read nothing" (or "nobody filled this field
   in"), and writing that back would pin the package at 0 W - so it is refused
   HERE, before any write. */
static int validate_stock(const stock_t *s, char *why, size_t n) {
    if (!pkg_dir[0]) {
        snprintf(why, n, "RAPL package zone not found: no /sys/class/powercap/intel-rapl* "
                         "exposes a zone named 'package-*' on this machine");
        return -1;
    }
    if (s->pl1_uw <= 0) {
        snprintf(why, n, "refusing to write pl1_uw=%ld: zero or negative is not a power limit "
                         "(re-capture the stock with 'capture --force')", s->pl1_uw);
        return -1;
    }
    if (s->pl2_uw <= 0) {
        snprintf(why, n, "refusing to write pl2_uw=%ld: zero or negative is not a power limit "
                         "(re-capture the stock with 'capture --force')", s->pl2_uw);
        return -1;
    }
    if (s->pl1_uw > PL_LIMIT_MAX_UW || s->pl2_uw > PL_LIMIT_MAX_UW) {
        snprintf(why, n, "refusing to write an implausible power limit (%ld/%ld uW, ceiling %ld uW)",
                 s->pl1_uw, s->pl2_uw, PL_LIMIT_MAX_UW);
        return -1;
    }
    if (core_dir[0]) {
        if (s->pp0_uw <= 0) {
            snprintf(why, n, "refusing to write pp0_uw=%ld: zero or negative is not a power "
                             "limit (re-capture the stock with 'capture --force')", s->pp0_uw);
            return -1;
        }
        if (s->pp0_uw > PL_LIMIT_MAX_UW) {
            snprintf(why, n, "refusing to write an implausible PP0 limit (%ld uW, ceiling %ld uW)",
                     s->pp0_uw, PL_LIMIT_MAX_UW);
            return -1;
        }
    }
    if (s->pl2_win_us < 0) {
        snprintf(why, n, "refusing to write a negative PL2 time window (%ld us)", s->pl2_win_us);
        return -1;
    }
    if (s->gt_min < 0) {
        snprintf(why, n, "refusing to write a negative GT min frequency (%ld MHz)", s->gt_min);
        return -1;
    }
    return 0;
}

static int apply_stock(const stock_t *s) {
    char p[MAX_PATH], v[64], why[NOTE_LEN];
    int fails = 0;
    if (validate_stock(s, why, sizeof why) != 0) {  /* defensive: main already checked it */
        warnf("%s", why);
        return -1;
    }
    JOIN(p, pkg_dir, "/constraint_0_power_limit_uw");
    snprintf(v, sizeof v, "%ld", s->pl1_uw);
    if (write_text(p, v) != 0) fails++;
    JOIN(p, pkg_dir, "/constraint_1_power_limit_uw");
    snprintf(v, sizeof v, "%ld", s->pl2_uw);
    if (write_text(p, v) != 0) fails++;
    /* burst window: only give it back if it was captured (>0). An old
       stock.conf, without the key, falls out here instead of writing 0 and
       killing the firmware burst. */
    if (s->pl2_win_us > 0) {
        JOIN(p, pkg_dir, "/constraint_1_time_window_us");
        snprintf(v, sizeof v, "%ld", s->pl2_win_us);
        if (write_text(p, v) != 0) fails++;
    }
    if (core_dir[0]) {
        JOIN(p, core_dir, "/constraint_0_power_limit_uw");
        snprintf(v, sizeof v, "%ld", s->pp0_uw);
        if (write_text(p, v) != 0) fails++;
        JOIN(p, core_dir, "/enabled");
        snprintf(v, sizeof v, "%d", s->pp0_en);
        if (write_text(p, v) != 0) fails++;
    } else {
        warnf("no RAPL subzone named 'core' (PP0): the stock PP0 limit was not restored");
    }
    if (s->epp[0] && epp_n > 0) {
        for (int i = 0; i < epp_n; i++)
            if (write_text(epp_paths[i], s->epp) != 0) fails++;
    } else {
        warnf("EPP not restored (%s): the stock has no EPP value or the machine has no "
              "cpufreq energy_performance_preference",
              s->epp[0] ? "no policy exposes it" : "not captured");
    }
    if (s->thermal[0] && platform_path[0]) {
        if (write_text(platform_path, s->thermal) != 0) fails++;
    } else {
        warnf("platform_profile not restored (%s)", s->thermal[0] ? "not published by this firmware"
                                                                  : "not captured");
    }
    if (gt_min_path[0] && s->gt_min > 0) {
        snprintf(v, sizeof v, "%ld", s->gt_min);
        if (write_text(gt_min_path, v) != 0) fails++;
    } else {
        warnf("GT min frequency not restored (%s)", gt_min_path[0] ? "not captured"
                                                                    : "no iGT GT directory");
    }
    return fails;
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(); return 2; }
    discover();
    const char *cmd = argv[1];

    /* discovery writes nothing - it can run unprivileged (useful for testing) */
    if (!strcmp(cmd, "paths")) { cmd_paths(); return pkg_n ? 0 : 1; }

    /* 'check' validates the plan without writing; 'show' only reads the stock */
    if (geteuid() != 0 && strcmp(cmd, "check") != 0 && strcmp(cmd, "show") != 0) {
        fprintf(stderr, "throttlefed-helper: needs root (use pkexec)\n");
        return 3;
    }

    if (!strcmp(cmd, "show")) {
        stock_t s;
        if (load_stock(&s) != 0) {
            printf("{\"ok\":false,\"error\":\"no stock at " STOCK_PATH "\"}\n");
            return 1;
        }
        stock_json(&s, "stock");
        return 0;
    }

    if (!strcmp(cmd, "capture")) {
        int force = (argc > 2 && strcmp(argv[2], "--force") == 0);
        stock_t s;
        if (!force && load_stock(&s) == 0) {
            stock_json(&s, "stock");
            printf("{\"captured\":false,\"ok\":true,\"reason\":\"already existed\"}\n");
            return 0;
        }
        char why[NOTE_LEN];
        if (read_stock_now(&s, why, sizeof why) != 0) {
            /* never save a stock with a zero by mistake: no reading, no stock */
            printf("{\"ok\":false,\"captured\":false,\"error\":");
            json_str(why);
            print_notes();
            printf("}\n");
            return 1;
        }
        if (save_stock(&s, why, sizeof why) != 0) {
            printf("{\"ok\":false,\"captured\":false,\"error\":");
            json_str(why);
            print_notes();
            printf("}\n");
            return 1;
        }
        stock_json(&s, "stock");
        printf("{\"captured\":true,\"ok\":true");
        print_notes();
        printf("}\n");
        return 0;
    }

    if (!strcmp(cmd, "restore")) {
        stock_t s;
        if (load_stock(&s) != 0) {
            printf("{\"ok\":false,\"error\":\"no saved stock\"}\n");
            return 1;
        }
        char why[NOTE_LEN];
        if (validate_stock(&s, why, sizeof why) != 0) {
            printf("{\"ok\":false,\"restored\":false,\"fails\":0,\"error\":");
            json_str(why);
            print_notes();
            printf("}\n");
            return 1;
        }
        int fails = apply_stock(&s);
        if (fails >= 0) write_active("stock");
        stock_json(&s, "stock");
        printf("{\"restored\":true,\"ok\":%s,\"fails\":%d", fails ? "false" : "true", fails);
        print_notes();
        printf("}\n");
        return fails ? 1 : 0;
    }

    if (!strcmp(cmd, "sample")) {
        long secs = 5, iv = 500;
        for (int i = 2; i + 1 < argc; i += 2) {
            if (!strcmp(argv[i], "--seconds")) secs = strtol(argv[i + 1], NULL, 10);
            else if (!strcmp(argv[i], "--interval")) iv = strtol(argv[i + 1], NULL, 10);
        }
        if (secs < 1) secs = 1;
        if (secs > 120) secs = 120;
        if (iv < 50) iv = 50;
        return cmd_sample(secs, iv);
    }

    int dryrun = !strcmp(cmd, "check");
    const char *active_name = "";
    for (int i = 2; i + 1 < argc; i++)
        if (!strcmp(argv[i], "--profile")) active_name = argv[i + 1];
    if (!strcmp(cmd, "apply") || dryrun) {
        char line[1024];
        size_t cap = 64;
        item_t *items = malloc(sizeof(*items) * cap);
        int n = 0, bad = 0;
        if (!items) {
            printf("{\"ok\":false,\"error\":\"out of memory for the plan\"}\n");
            return 1;
        }
        /* phase 1: read and classify the WHOLE plan (nothing is written yet) */
        while (fgets(line, sizeof line, stdin)) {
            char *nl = strchr(line, '\n');
            if (nl) *nl = 0;
            if (line[0] == 0 || line[0] == '#') continue;
            if ((size_t)n == cap) {
                if (cap >= MAX_PLAN_ITEMS) {
                    /* do not truncate silently and answer ok: a plan bigger
                       than this is refused whole, with no partial write */
                    printf("{\"ok\":false,\"dry\":%d,\"n\":%d,\"error\":\"plan has more than "
                           "%d items; refusing the whole plan (nothing was written)\"}\n",
                           dryrun, n, MAX_PLAN_ITEMS);
                    free(items);
                    return 2;
                }
                size_t ncap = cap * 2;
                if (ncap > MAX_PLAN_ITEMS) ncap = MAX_PLAN_ITEMS;
                item_t *nu = realloc(items, sizeof(*items) * ncap);
                if (!nu) {
                    printf("{\"ok\":false,\"error\":\"out of memory growing the plan to %zu items\"}\n", ncap);
                    free(items);
                    return 1;
                }
                items = nu;
                cap = ncap;
            }
            char *tab = strchr(line, '\t');
            if (!tab) {
                item_t *it = &items[n++];
                memset(it, 0, sizeof *it);
                snprintf(it->target, sizeof it->target, "%.480s", line);
                it->rejected = 1;
                it->err = EINVAL;
                snprintf(it->reason, sizeof it->reason, "malformed line (no <TAB> between target and value)");
                bad++;
                continue;
            }
            *tab = 0;
            char *val = tab + 1;
            /* The target can be a real path (it goes through the discovered
               whitelist) or one of the pseudo-targets @epp, @platform_profile,
               @pp0, @fwa:. The VALUE is validated separately: no space, ';',
               '$', slash or newline. */
            int is_pseudo = (!strcmp(line, "@epp") || !strcmp(line, "@platform_profile") ||
                             !strcmp(line, "@pp0") || !strncmp(line, "@fwa:", 5));
            int target_ok = is_pseudo || path_allowed(line);
            /* @fwa: the value has to be one of the ones the firmware published */
            int value_ok = !strncmp(line, "@fwa:", 5) ? fwa_value_allowed(line, val)
                                                      : value_allowed(val);
            item_t *it = &items[n++];
            memset(it, 0, sizeof *it);
            snprintf(it->target, sizeof it->target, "%.480s", line);
            snprintf(it->wrote, sizeof it->wrote, "%.120s", val);
            if (!target_ok) {
                it->rejected = 1;
                it->err = EPERM;
                if (is_pseudo) snprintf(it->reason, sizeof it->reason, "unknown pseudo-target");
                else target_reason(line, it->reason, sizeof it->reason);
                bad++;
                continue;
            }
            if (!value_ok) {
                it->rejected = 2;
                it->err = EPERM;
                if (!strncmp(line, "@fwa:", 5))
                    snprintf(it->reason, sizeof it->reason,
                             "value is not among the values published in possible_values "
                             "(that list is the only source of accepted values)");
                else
                    snprintf(it->reason, sizeof it->reason,
                             "value has characters outside [A-Za-z0-9_-] or is too long");
                bad++;
                continue;
            }
        }

        /* phase 2: a domain missing on this machine is not an invalid item -
           stop and say which one */
        const char *gap = plan_gap(items, n);
        if (gap) {
            printf("{\"ok\":false,\"dry\":%d,\"n\":%d,\"bad\":%d,\"error\":", dryrun, n, bad);
            json_str(gap);
            print_notes();
            printf("}\n");
            free(items);
            return 2;
        }

        /* phase 3: write (or only resolve, under check) */
        for (int i = 0; i < n; i++) {
            item_t *it = &items[i];
            if (it->rejected) continue;
            if (write_one(it, dryrun) != 0) bad++;
        }
        if (!bad && active_name[0] && !dryrun) write_active(active_name);

        printf("{\"ok\":%s,\"dry\":%d,\"n\":%d,\"bad\":%d,\"items\":[",
               bad ? "false" : "true", dryrun, n, bad);
        for (int i = 0; i < n; i++) {
            item_t *it = &items[i];
            if (i) printf(",");
            printf("{\"target\":");
            json_str(it->target);
            printf(",\"path\":");
            json_str(it->real);
            printf(",\"wrote\":");
            json_str(it->wrote);
            printf(",\"readback\":");
            json_str(it->readback);
            printf(",\"ok\":%s,\"err\":", it->ok ? "true" : "false");
            if (it->ok) json_str("ok");
            else if (it->rejected == 1) json_str("REJECTED: target outside the whitelist");
            else if (it->rejected == 2) json_str("REJECTED: invalid value");
            else json_str(errname(it->err));
            if (it->reason[0]) { printf(",\"reason\":"); json_str(it->reason); }
            printf("}");
        }
        printf("]");
        print_notes();
        printf("}\n");
        free(items);
        return bad ? 1 : 0;
    }

    usage();
    return 2;
}
