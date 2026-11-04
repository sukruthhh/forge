#include "forge/executor.hpp"
#include "forge/network.hpp"
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <spawn.h>
#include <stdexcept>
#include <sys/wait.h>
#include <unistd.h>

extern char** environ;

namespace forge {
namespace {
constexpr std::size_t output_limit = 16384;
struct Pipe {
    net::Socket read;
    net::Socket write;
    Pipe() {
        int fds[2];
        if (::pipe2(fds, O_CLOEXEC) != 0) throw std::runtime_error("pipe creation failed");
        read = net::Socket(fds[0]);
        write = net::Socket(fds[1]);
        const int flags = ::fcntl(read.get(), F_GETFL);
        if (flags < 0 || ::fcntl(read.get(), F_SETFL, flags | O_NONBLOCK) < 0)
            throw std::runtime_error("pipe configuration failed");
    }
};
struct SpawnConfig {
    posix_spawn_file_actions_t actions;
    posix_spawnattr_t attributes;
    SpawnConfig() {
        if (::posix_spawn_file_actions_init(&actions) != 0)
            throw std::runtime_error("spawn actions initialization failed");
        if (::posix_spawnattr_init(&attributes) != 0) {
            ::posix_spawn_file_actions_destroy(&actions);
            throw std::runtime_error("spawn attributes initialization failed");
        }
    }
    ~SpawnConfig() {
        ::posix_spawn_file_actions_destroy(&actions);
        ::posix_spawnattr_destroy(&attributes);
    }
};
struct Child {
    pid_t pid;
    bool reaped = false;
    ~Child() {
        if (!reaped) {
            ::kill(-pid, SIGKILL);
            while (::waitpid(pid, nullptr, 0) < 0 && errno == EINTR) {}
        }
    }
};
void checked(int error) {
    if (error != 0) throw std::runtime_error(std::string("spawn setup: ") + std::strerror(error));
}
bool drain(int fd, std::string& destination, bool& truncated) {
    char bytes[4096];
    // Bound work per tick: a continuously noisy child cannot starve deadlines.
    for (int i = 0; i < 16; ++i) {
        const auto count = ::read(fd, bytes, sizeof(bytes));
        if (count == 0) return false;
        if (count < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) return true;
            throw std::runtime_error("output pipe read failed");
        }
        const auto keep = std::min(static_cast<std::size_t>(count), output_limit - destination.size());
        destination.append(bytes, keep);
        if (keep < static_cast<std::size_t>(count)) truncated = true;
    }
    return true;
}
}

ExecutionResult execute(const std::vector<std::string>& arguments,
                        std::uint32_t timeout_seconds, const std::atomic<bool>& stop,
                        const std::string& working_directory) {
    if (arguments.empty() || arguments.front().empty() || timeout_seconds == 0 || timeout_seconds > 30)
        throw std::runtime_error("invalid execution request");
    std::vector<char*> argv;
    for (const auto& argument : arguments) {
        if (argument.find('\0') != std::string::npos) throw std::runtime_error("NUL in argument");
        argv.push_back(const_cast<char*>(argument.c_str()));
    }
    argv.push_back(nullptr);
    Pipe out;
    Pipe err;
    SpawnConfig config;
    if (!working_directory.empty())
        checked(::posix_spawn_file_actions_addchdir_np(&config.actions, working_directory.c_str()));
    checked(::posix_spawn_file_actions_addopen(&config.actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0));
    checked(::posix_spawn_file_actions_adddup2(&config.actions, out.write.get(), STDOUT_FILENO));
    checked(::posix_spawn_file_actions_adddup2(&config.actions, err.write.get(), STDERR_FILENO));
    sigset_t defaults;
    ::sigemptyset(&defaults);
    ::sigaddset(&defaults, SIGPIPE);
    checked(::posix_spawnattr_setsigdefault(&config.attributes, &defaults));
    checked(::posix_spawnattr_setflags(&config.attributes, POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGDEF));
    checked(::posix_spawnattr_setpgroup(&config.attributes, 0));
    pid_t pid = -1;
    const int error = ::posix_spawnp(&pid, argv.front(), &config.actions, &config.attributes, argv.data(), environ);
    ExecutionResult result;
    if (error) {
        result.exit_code = 127;
        result.error = std::string("cannot execute: ") + std::strerror(error) + '\n';
        return result;
    }
    Child child{pid};
    out.write = net::Socket();
    err.write = net::Socket();
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout_seconds);
    bool out_open = true, err_open = true;
    bool exited = false;
    int status = 0;
    while (!exited || out_open || err_open) {
        if (stop.load() || std::chrono::steady_clock::now() >= deadline) {
            result.timed_out = !stop.load();
            ::kill(-pid, SIGKILL);
            while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
            child.reaped = true;
            if (out_open) drain(out.read.get(), result.output, result.truncated);
            if (err_open) drain(err.read.get(), result.error, result.truncated);
            result.exit_code = result.timed_out ? 124 : 130;
            return result;
        }
        pollfd pipes[2]{{out_open ? out.read.get() : -1, POLLIN, 0},
                        {err_open ? err.read.get() : -1, POLLIN, 0}};
        const int ready = ::poll(pipes, 2, 50);
        if (ready < 0 && errno != EINTR) throw std::runtime_error("output poll failed");
        if (out_open) out_open = drain(out.read.get(), result.output, result.truncated);
        if (err_open) err_open = drain(err.read.get(), result.error, result.truncated);
        if (!exited) {
            // Observe without reaping. Retaining the leader's PID prevents PID
            // reuse before the final process-group cleanup.
            siginfo_t info{};
            if (::waitid(P_PID, static_cast<id_t>(pid), &info, WEXITED | WNOHANG | WNOWAIT) == 0)
                exited = info.si_pid == pid;
            else if (errno != EINTR) throw std::runtime_error("waitid failed");
        }
    }
    ::kill(-pid, SIGKILL);
    while (::waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) throw std::runtime_error("waitpid failed");
    }
    child.reaped = true;
    result.exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
    return result;
}
}
