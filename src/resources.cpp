#include "forge/resources.hpp"
#include "forge/executor.hpp"
#include <algorithm>
#include <atomic>
#include <bit>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <linux/magic.h>
#include <sched.h>
#include <sstream>
#include <sys/stat.h>
#include <sys/sysinfo.h>
#include <sys/vfs.h>
#include <thread>
#include <unistd.h>

namespace forge {
namespace {
namespace fs = std::filesystem;
std::uint64_t number(const std::string& text) {
    std::uint64_t value = 0;
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
    if (error != std::errc{} || end != text.data() + text.size())
        throw std::runtime_error("invalid cgroup resource value");
    return value;
}
std::string read(const fs::path& path, bool optional = false) {
    std::ifstream file(path);
    if (!file) {
        if (optional && !fs::exists(path)) return {};
        throw std::runtime_error("cannot read resource control: " + path.string());
    }
    std::string text((std::istreambuf_iterator<char>(file)), {});
    if (file.bad()) throw std::runtime_error("resource control read failed");
    return text;
}
std::string token(const std::string& text) {
    std::istringstream input(text);
    std::string value, extra;
    if (!(input >> value) || (input >> extra)) throw std::runtime_error("invalid cgroup single value");
    return value;
}
bool beneath(const fs::path& path, const fs::path& root) {
    auto p = path.begin();
    for (auto r = root.begin(); r != root.end(); ++r, ++p)
        if (p == path.end() || *p != *r) return false;
    return true;
}
std::string unescape(const std::string& value) {
    std::string decoded;
    for (std::size_t i = 0; i < value.size(); ++i) {
        if (value[i] == '\\' && i + 3 < value.size() &&
            std::all_of(value.begin() + i + 1, value.begin() + i + 4, [](char c) { return c >= '0' && c <= '7'; })) {
            decoded += static_cast<char>((value[i + 1] - '0') * 64 + (value[i + 2] - '0') * 8 + value[i + 3] - '0');
            i += 3;
        } else decoded += value[i];
    }
    return decoded;
}
std::pair<fs::path, fs::path> cgroup_location() {
    std::istringstream memberships(read("/proc/self/cgroup"));
    std::string line, unified;
    while (std::getline(memberships, line))
        if (line.starts_with("0::")) unified = line.substr(3);
    const fs::path group(unified);
    if (!group.is_absolute() || std::find(group.begin(), group.end(), "..") != group.end())
        throw std::runtime_error("resource discovery requires a visible unified cgroup v2 hierarchy");
    std::istringstream mounts(read("/proc/self/mountinfo"));
    while (std::getline(mounts, line)) {
        std::istringstream row(line);
        std::vector<std::string> fields;
        for (std::string field; row >> field;) fields.push_back(field);
        const auto separator = std::find(fields.begin(), fields.end(), "-");
        if (fields.size() < 7 || separator == fields.end() || separator + 1 == fields.end() || separator[1] != "cgroup2") continue;
        const fs::path root(unescape(fields[3])), mount(unescape(fields[4]));
        if (!beneath(group, root)) continue;
        const auto current = (mount / group.lexically_relative(root)).lexically_normal();
        if (fs::exists(current / "cgroup.procs")) return {mount.lexically_normal(), current};
    }
    throw std::runtime_error("cannot resolve worker cgroup v2 mount");
}
std::string control(int directory, const char* name) {
    net::Socket file(::openat(directory, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
    if (file.get() < 0) throw std::runtime_error(std::string("cannot read cgroup control: ") + name);
    std::string result;
    char bytes[4096];
    for (;;) {
        const auto count = ::read(file.get(), bytes, sizeof(bytes));
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) throw std::runtime_error("cgroup control read failed");
        if (count == 0) return result;
        result.append(bytes, static_cast<std::size_t>(count));
        if (result.size() > 65536) throw std::runtime_error("cgroup control exceeds read bound");
    }
}
void write_control(int directory, const char* name, const std::string& value) {
    net::Socket file(::openat(directory, name, O_WRONLY | O_CLOEXEC | O_NOFOLLOW));
    if (file.get() < 0) throw std::runtime_error(std::string("cannot open cgroup control: ") + name + ": " + std::strerror(errno));
    ssize_t count;
    do { count = ::write(file.get(), value.data(), value.size()); } while (count < 0 && errno == EINTR);
    if (count != static_cast<ssize_t>(value.size()))
        throw std::runtime_error(std::string("cannot write cgroup control: ") + name + ": " + std::strerror(errno));
}
std::uint64_t keyed(const std::string& text, const std::string& key) {
    std::istringstream input(text);
    for (std::string field, value; input >> field >> value;)
        if (field == key) return number(value);
    throw std::runtime_error("missing cgroup metric: " + key);
}
}

Resources constrain_capacity(Resources capacity, const fs::path& leaf, const fs::path& boundary) {
    const auto root = fs::canonical(boundary);
    auto current = fs::canonical(leaf);
    if (!beneath(current, root)) throw std::runtime_error("cgroup path escapes visible hierarchy");
    for (;;) {
        const auto cpu = read(current / "cpu.max", true);
        if (!cpu.empty()) {
            std::istringstream input(cpu);
            std::string quota, period, extra;
            if (!(input >> quota >> period) || (input >> extra) || number(period) == 0)
                throw std::runtime_error("invalid cpu.max");
            if (quota != "max") {
                const auto value = number(quota);
                if (value == 0) throw std::runtime_error("invalid zero CPU quota");
                capacity.cpu = static_cast<std::uint32_t>(std::min<std::uint64_t>(capacity.cpu, value / number(period)));
            }
        }
        const auto memory = read(current / "memory.max", true);
        if (!memory.empty() && token(memory) != "max") {
            const auto limit = number(token(memory));
            const auto used = number(token(read(current / "memory.current")));
            capacity.memory_bytes = std::min(capacity.memory_bytes, limit > used ? limit - used : 0);
        }
        if (current == root) return capacity;
        current = current.parent_path();
    }
}

Resources resource_capacity(const fs::path& additional) {
    std::vector<unsigned long> affinity(16);
    while (::sched_getaffinity(0, affinity.size() * sizeof(unsigned long), reinterpret_cast<cpu_set_t*>(affinity.data())) != 0) {
        if (errno != EINVAL || affinity.size() >= 16384) throw std::runtime_error("CPU affinity discovery failed");
        affinity.resize(affinity.size() * 2);
    }
    std::uint32_t cpus = 0;
    for (const auto bits : affinity) cpus += static_cast<std::uint32_t>(std::popcount(bits));
    struct sysinfo memory{};
    if (::sysinfo(&memory) != 0) throw std::runtime_error("memory discovery failed");
    auto bytes = static_cast<std::uint64_t>(memory.totalram) * memory.mem_unit;
    std::istringstream meminfo(read("/proc/meminfo"));
    std::string line;
    while (std::getline(meminfo, line)) {
        std::istringstream row(line);
        std::string key, value, unit;
        if (row >> key >> value >> unit && key == "MemAvailable:" && unit == "kB")
            bytes = std::min(bytes, number(value) * 1024);
    }
    const auto [mount, current] = cgroup_location();
    auto capacity = constrain_capacity({cpus, bytes}, current, mount);
    if (!additional.empty()) capacity = constrain_capacity(capacity, additional, mount);
    if (capacity.cpu == 0) throw std::runtime_error("less than one whole CPU is available; fractional CPU requests are not supported");
    if (capacity.memory_bytes == 0) throw std::runtime_error("no memory headroom is available");
    return capacity;
}

CgroupRoot::CgroupRoot(const std::string& path)
    : directory_(::open(fs::canonical(path).c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)) {
    struct statfs filesystem{};
    if (directory_.get() < 0 || ::fstatfs(directory_.get(), &filesystem) != 0 || filesystem.f_type != CGROUP2_SUPER_MAGIC)
        throw std::runtime_error("cgroup root must be a delegated cgroup v2 directory");
    const auto enabled = control(directory_.get(), "cgroup.subtree_control");
    std::istringstream input(enabled);
    bool cpu = false, memory = false;
    for (std::string field; input >> field;) { cpu |= field == "cpu"; memory |= field == "memory"; }
    if (!cpu || !memory || token(control(directory_.get(), "cgroup.type")) != "domain")
        throw std::runtime_error("cgroup root requires enabled cpu/memory controllers in an empty domain; provision delegation first");
    CgroupJob probe(*this, {1, 32 * 1024 * 1024});
    const std::atomic<bool> cancelled{false};
    const auto result = execute({"/bin/true"}, 2, cancelled, {}, &probe);
    if (result.exit_code != 0)
        throw std::runtime_error("cgroup delegation cannot launch a confined probe (exit " +
                                 std::to_string(result.exit_code) + "): " + result.error);
    probe.cleanup();
}

CgroupJob::CgroupJob(const CgroupRoot& root, Resources resources) : root_(root) {
    if (resources.cpu == 0 || resources.cpu > 1048576) throw std::runtime_error("invalid cgroup CPU limit");
    static std::atomic<std::uint64_t> next{0};
    for (int retry = 0; retry < 1024; ++retry) {
        name_ = "forge-" + std::to_string(::getpid()) + '-' + std::to_string(next++);
        if (::mkdirat(root_.directory_.get(), name_.c_str(), 0700) == 0) break;
        if (errno != EEXIST || retry == 1023) throw std::runtime_error("cannot create job cgroup: " + std::string(std::strerror(errno)));
    }
    try {
        directory_ = net::Socket(::openat(root_.directory_.get(), name_.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
        if (directory_.get() < 0) throw std::runtime_error("cannot open job cgroup");
        write_control(directory_.get(), "cpu.max", std::to_string(static_cast<std::uint64_t>(resources.cpu) * 100000) + " 100000");
        write_control(directory_.get(), "memory.max", resources.memory_bytes == 0 ? "max" : std::to_string(resources.memory_bytes));
        write_control(directory_.get(), "memory.swap.max", resources.memory_bytes == 0 ? "max" : "0");
        write_control(directory_.get(), "memory.oom.group", "1");
        write_control(directory_.get(), "cgroup.kill", "1");
    } catch (...) {
        if (::unlinkat(root_.directory_.get(), name_.c_str(), AT_REMOVEDIR) != 0)
            throw ResourceCleanupError("failed resource setup left a job cgroup; worker must stop");
        throw;
    }
}
CgroupJob::~CgroupJob() {
    if (!cleaned_) try { cleanup(); }
    catch (const std::exception& error) { std::cerr << "[forge] " << error.what() << '\n'; }
}
void CgroupJob::kill() const { write_control(directory_.get(), "cgroup.kill", "1"); }
bool CgroupJob::oom_killed() const { return keyed(control(directory_.get(), "memory.events"), "oom_kill") != 0; }
void CgroupJob::cleanup() {
    if (cleaned_) return;
    try {
        kill();
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
        while (keyed(control(directory_.get(), "cgroup.events"), "populated") != 0) {
            if (std::chrono::steady_clock::now() >= deadline) throw std::runtime_error("job cgroup remains populated");
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        if (::unlinkat(root_.directory_.get(), name_.c_str(), AT_REMOVEDIR) != 0)
            throw std::runtime_error("cannot remove job cgroup: " + std::string(std::strerror(errno)));
        cleaned_ = true;
    } catch (const std::exception& error) { throw ResourceCleanupError(error.what()); }
}

int cgroup_exec(int descriptor, char* const arguments[]) {
    try {
        struct statfs filesystem{};
        if (descriptor < 3 || !arguments[0] || ::fstatfs(descriptor, &filesystem) != 0 || filesystem.f_type != CGROUP2_SUPER_MAGIC)
            throw std::runtime_error("invalid cgroup execution descriptor");
        sched_param scheduling{};
        if (::sched_setscheduler(0, SCHED_OTHER, &scheduling) != 0)
            throw std::runtime_error("cannot select normal CPU scheduling for limited job");
        write_control(descriptor, "cgroup.procs", std::to_string(::getpid()));
        if (::fcntl(descriptor, F_SETFD, FD_CLOEXEC) != 0) throw std::runtime_error("cannot protect cgroup descriptor");
        ::execvp(arguments[0], arguments);
        std::cerr << "cannot execute: " << std::strerror(errno) << '\n';
        return 127;
    } catch (const std::exception& error) {
        std::cerr << "cgroup execution setup failed: " << error.what() << '\n';
        return 125;
    }
}
}
