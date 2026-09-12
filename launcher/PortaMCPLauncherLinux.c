#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <dirent.h>

static char root_dir[PATH_MAX];
static FILE *progress_stream = NULL;
static pid_t progress_pid = -1;

static bool is_executable(const char *path) {
    return path && access(path, X_OK) == 0;
}

static bool is_regular_file(const char *path) {
    struct stat st;
    return path && stat(path, &st) == 0 && S_ISREG(st.st_mode);
}

static bool is_directory(const char *path) {
    struct stat st;
    return path && stat(path, &st) == 0 && S_ISDIR(st.st_mode);
}

static void join_path(char *out, size_t size, const char *a, const char *b) {
    if (snprintf(out, size, "%s/%s", a, b) >= (int)size) {
        fprintf(stderr, "PortaMCP path is too long.\n");
        exit(1);
    }
}

static bool resolve_root(void) {
    char exe[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (n <= 0 || n >= (ssize_t)sizeof(exe)) return false;
    exe[n] = '\0';
    char *slash = strrchr(exe, '/');
    if (!slash) return false;
    *slash = '\0';
    if (strlen(exe) >= sizeof(root_dir)) return false;
    strcpy(root_dir, exe);
    return true;
}

static char *find_in_path(const char *name) {
    if (!name || !*name) return NULL;
    if (strchr(name, '/')) return is_executable(name) ? strdup(name) : NULL;
    const char *path = getenv("PATH");
    if (!path) return NULL;
    char *copy = strdup(path);
    if (!copy) return NULL;
    char *save = NULL;
    for (char *part = strtok_r(copy, ":", &save); part; part = strtok_r(NULL, ":", &save)) {
        char candidate[PATH_MAX];
        const char *base = *part ? part : ".";
        if (snprintf(candidate, sizeof(candidate), "%s/%s", base, name) >= (int)sizeof(candidate)) continue;
        if (is_executable(candidate)) {
            char *result = strdup(candidate);
            free(copy);
            return result;
        }
    }
    free(copy);
    return NULL;
}

static int run_process(char *const argv[], const char *log_path, bool quiet) {
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        if (chdir(root_dir) != 0) _exit(126);
        if (log_path) {
            FILE *log = fopen(log_path, "a");
            if (log) {
                dup2(fileno(log), STDOUT_FILENO);
                dup2(fileno(log), STDERR_FILENO);
            }
        } else if (quiet) {
            FILE *nullf = fopen("/dev/null", "w");
            if (nullf) {
                dup2(fileno(nullf), STDOUT_FILENO);
                dup2(fileno(nullf), STDERR_FILENO);
            }
        }
        execvp(argv[0], argv);
        _exit(127);
    }
    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return -1;
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return 128;
}

static bool spawn_detached(char *const argv[]) {
    pid_t pid = fork();
    if (pid < 0) return false;
    if (pid == 0) {
        if (setsid() < 0) _exit(126);
        if (chdir(root_dir) != 0) _exit(126);
        FILE *nullf = fopen("/dev/null", "r+");
        if (nullf) {
            dup2(fileno(nullf), STDIN_FILENO);
            dup2(fileno(nullf), STDOUT_FILENO);
            dup2(fileno(nullf), STDERR_FILENO);
        }
        execv(argv[0], argv);
        _exit(127);
    }
    return true;
}

static bool python_bootstrap_usable(const char *python) {
    if (!python || !*python) return false;
    char *const argv[] = {
        (char *)python,
        (char *)"-c",
        (char *)"import sys, tkinter, venv, ensurepip; raise SystemExit(0 if sys.version_info >= (3, 11) else 9)",
        NULL
    };
    return run_process(argv, NULL, true) == 0;
}

static char *find_bootstrap_python(void) {
    const char *requested = getenv("PYTHON");
    if (requested && python_bootstrap_usable(requested)) return strdup(requested);
    const char *names[] = {"python3.14", "python3.13", "python3.12", "python3.11", "python3", "python", NULL};
    for (int i = 0; names[i]; ++i) {
        char *candidate = find_in_path(names[i]);
        if (candidate && python_bootstrap_usable(candidate)) return candidate;
        free(candidate);
    }
    return NULL;
}

static bool atspi_typelib_present(void) {
    const char *direct[] = {
        "/usr/lib/girepository-1.0/Atspi-2.0.typelib",
        "/usr/local/lib/girepository-1.0/Atspi-2.0.typelib",
        NULL
    };
    for (int i = 0; direct[i]; ++i) if (is_regular_file(direct[i])) return true;

    const char *roots[] = {"/usr/lib", "/usr/local/lib", NULL};
    for (int r = 0; roots[r]; ++r) {
        DIR *dir = opendir(roots[r]);
        if (!dir) continue;
        struct dirent *entry;
        while ((entry = readdir(dir)) != NULL) {
            char candidate[PATH_MAX];
            int n = snprintf(candidate, sizeof(candidate), "%s/%s/girepository-1.0/Atspi-2.0.typelib", roots[r], entry->d_name);
            if (n > 0 && n < (int)sizeof(candidate) && is_regular_file(candidate)) {
                closedir(dir);
                return true;
            }
        }
        closedir(dir);
    }
    return false;
}

static bool atspi_runtime_present(void) {
    bool gi = is_directory("/usr/lib/python3/dist-packages/gi") || is_directory("/usr/local/lib/python3/dist-packages/gi");
    return gi && atspi_typelib_present();
}

static bool command_present(const char *name) {
    char *path = find_in_path(name);
    bool ok = path != NULL;
    free(path);
    return ok;
}

static bool os_prerequisites_ready(void) {
    char *python = find_bootstrap_python();
    bool ok = python != NULL && atspi_runtime_present() && command_present("git") && command_present("zenity");
    free(python);
    return ok;
}

static void show_zenity_error(const char *message) {
    char text[4096];
    snprintf(text, sizeof(text), "%s", message ? message : "PortaMCP setup failed.");
    char *const argv[] = {
        (char *)"zenity", (char *)"--error", (char *)"--title=PortaMCP Setup",
        (char *)"--width=560", (char *)"--text", text, NULL
    };
    if (command_present("zenity")) (void)run_process(argv, NULL, true);
    fprintf(stderr, "%s\n", text);
}

static void progress_start(void) {
    if (!command_present("zenity")) return;
    int pipefd[2];
    if (pipe(pipefd) != 0) return;
    pid_t pid = fork();
    if (pid < 0) {
        close(pipefd[0]); close(pipefd[1]); return;
    }
    if (pid == 0) {
        close(pipefd[1]);
        dup2(pipefd[0], STDIN_FILENO);
        close(pipefd[0]);
        if (chdir(root_dir) != 0) _exit(126);
        execlp("zenity", "zenity", "--progress", "--pulsate", "--no-cancel", "--auto-close",
               "--title=PortaMCP Setup", "--text=Preparing PortaMCP...", "--width=560", (char *)NULL);
        _exit(127);
    }
    close(pipefd[0]);
    progress_pid = pid;
    progress_stream = fdopen(pipefd[1], "w");
    if (progress_stream) setvbuf(progress_stream, NULL, _IOLBF, 0);
}

static void progress_status(const char *text) {
    if (progress_stream) {
        fprintf(progress_stream, "# %s\n", text);
        fflush(progress_stream);
    }
}

static void progress_close(bool success) {
    if (progress_stream) {
        if (success) fprintf(progress_stream, "100\n");
        fclose(progress_stream);
        progress_stream = NULL;
    }
    if (progress_pid > 0) {
        int status = 0;
        for (int i = 0; i < 30; ++i) {
            pid_t got = waitpid(progress_pid, &status, WNOHANG);
            if (got == progress_pid) break;
            struct timespec pause = {0, 100000000L};
            nanosleep(&pause, NULL);
        }
        progress_pid = -1;
    }
}

static int install_os_prerequisites(void) {
    if (os_prerequisites_ready()) return 0;
    if (!command_present("pkexec")) return 70;

    int code = 71;
    if (command_present("apt-get")) {
        char *const argv[] = {
            (char *)"pkexec", (char *)"/bin/sh", (char *)"-c",
            (char *)"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-tk python3-gi gir1.2-atspi-2.0 git zenity",
            NULL
        };
        code = run_process(argv, NULL, false);
    } else if (command_present("dnf")) {
        char *const argv[] = {
            (char *)"pkexec", (char *)"dnf", (char *)"install", (char *)"-y",
            (char *)"python3", (char *)"python3-tkinter", (char *)"python3-gobject", (char *)"at-spi2-core", (char *)"git", (char *)"zenity", NULL
        };
        code = run_process(argv, NULL, false);
    } else if (command_present("pacman")) {
        char *const argv[] = {
            (char *)"pkexec", (char *)"pacman", (char *)"-S", (char *)"--noconfirm",
            (char *)"python", (char *)"tk", (char *)"python-gobject", (char *)"at-spi2-core", (char *)"git", (char *)"zenity", NULL
        };
        code = run_process(argv, NULL, false);
    } else if (command_present("zypper")) {
        char *const argv[] = {
            (char *)"pkexec", (char *)"zypper", (char *)"--non-interactive", (char *)"install",
            (char *)"python3", (char *)"python3-tk", (char *)"python3-gobject", (char *)"at-spi2-core", (char *)"git", (char *)"zenity", NULL
        };
        code = run_process(argv, NULL, false);
    }
    if (code != 0) return code;
    return os_prerequisites_ready() ? 0 : 72;
}

static bool environment_ready(void) {
    char python[PATH_MAX];
    join_path(python, sizeof(python), root_dir, ".venv-linux/bin/python");
    if (!is_executable(python)) return false;
    char *const argv[] = {
        python,
        (char *)"-c",
        (char *)"from pathlib import Path; import mcp, customtkinter, playwright, websockets, PIL, porta_mcp; from porta_mcp.platform_support import system_chromium_executable; ok=system_chromium_executable() is not None; exec(\"from playwright.sync_api import sync_playwright\\np=sync_playwright().start()\\nok=ok or Path(p.chromium.executable_path).is_file()\\np.stop()\") if not ok else None; raise SystemExit(0 if ok else 1)",
        NULL
    };
    return run_process(argv, NULL, true) == 0;
}

static int run_installer(const char *python) {
    char runtime[PATH_MAX];
    char log_path[PATH_MAX];
    join_path(runtime, sizeof(runtime), root_dir, ".portamcp");
    mkdir(runtime, 0700);
    join_path(log_path, sizeof(log_path), root_dir, ".portamcp/setup.log");
    FILE *clear = fopen(log_path, "w");
    if (clear) fclose(clear);
    char *const argv[] = {(char *)python, (char *)"-u", (char *)"-m", (char *)"porta_mcp.installer", NULL};
    return run_process(argv, log_path, false);
}

static bool launch_control_center(void) {
    char python[PATH_MAX];
    join_path(python, sizeof(python), root_dir, ".venv-linux/bin/python");
    if (!is_executable(python)) return false;
    char *const argv[] = {python, (char *)"-m", (char *)"porta_mcp.control_center", NULL};
    return spawn_detached(argv);
}

int main(void) {
    if (!resolve_root()) {
        fprintf(stderr, "PortaMCP could not resolve its application directory.\n");
        return 1;
    }
    if (geteuid() == 0) {
        show_zenity_error("Do not run PortaMCP as root. Launch it as your normal desktop user.");
        return 1;
    }

    char installer[PATH_MAX];
    join_path(installer, sizeof(installer), root_dir, "porta_mcp/installer.py");
    if (!is_regular_file(installer)) {
        show_zenity_error("The PortaMCP application files are incomplete. Extract the complete Linux release bundle and try again.");
        return 1;
    }

    if (environment_ready()) {
        if (!launch_control_center()) {
            show_zenity_error("The PortaMCP environment is ready, but the Control Center could not be launched.");
            return 1;
        }
        return 0;
    }

    if (command_present("zenity")) progress_start();
    progress_status("Checking Linux prerequisites...");

    int prereq = install_os_prerequisites();
    if (prereq != 0) {
        progress_close(false);
        show_zenity_error("PortaMCP could not install the required Linux prerequisites automatically. Administrator authorization may have been cancelled, or the package manager failed. Retry after checking your Internet connection.");
        return prereq;
    }

    if (!progress_stream) progress_start();
    progress_status("Creating the private virtual environment and installing dependencies...");

    char *python = find_bootstrap_python();
    if (!python) {
        progress_close(false);
        show_zenity_error("Python 3.11+ with venv and Tk is still unavailable after prerequisite setup.");
        return 73;
    }

    int install_code = run_installer(python);
    free(python);
    if (install_code != 0) {
        progress_close(false);
        show_zenity_error("PortaMCP dependency installation failed. Details were written to .portamcp/setup.log. Check your Internet connection and launch PortaMCP again to retry.");
        return install_code;
    }

    progress_status("Validating the completed PortaMCP environment...");
    if (!environment_ready()) {
        progress_close(false);
        show_zenity_error("Setup completed, but the private PortaMCP environment did not pass validation. Details are available in .portamcp/setup.log.");
        return 74;
    }

    progress_status("Setup complete. Opening PortaMCP...");
    if (!launch_control_center()) {
        progress_close(false);
        show_zenity_error("The Control Center could not be launched after setup.");
        return 75;
    }
    progress_close(true);
    return 0;
}
