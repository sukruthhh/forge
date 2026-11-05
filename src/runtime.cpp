#include "forge/runtime.hpp"
#include "forge/network.hpp"
#include "forge/executor.hpp"
#include "forge/scheduler.hpp"
#include "forge/resources.hpp"
#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <csignal>
#include <condition_variable>
#include <cstdlib>
#include <deque>
#include <future>
#include <iostream>
#include <map>
#include <limits>
#include <mutex>
#include <memory>
#include <optional>
#include <poll.h>
#include <stdexcept>
#include <sys/socket.h>
#include <sys/utsname.h>
#include <thread>
#include <unistd.h>

namespace forge {
namespace {
using Clock = std::chrono::steady_clock;
using net::Message;
using net::Type;
static_assert(std::atomic<bool>::is_always_lock_free);
std::atomic<bool> stopped{false};
void stop(int) { stopped.store(true, std::memory_order_relaxed); }
void install_signals() {
    stopped = 0;
    std::signal(SIGINT, stop);
    std::signal(SIGTERM, stop);
}
std::uint64_t number(const std::string& value) {
    std::uint64_t result = 0;
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (error != std::errc{} || end != value.data() + value.size())
        throw std::runtime_error("invalid resource number");
    return result;
}
bool label(const std::string& value) {
    return !value.empty() && value.size() <= 64 &&
        std::all_of(value.begin(), value.end(), [](unsigned char c) {
            return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                   (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
        });
}
bool cancellation_cleanup(const std::string& value) {
    return value == "reported" || value == "not-started" || value == "unconfirmed";
}
std::string configured_token() {
    const char* value = std::getenv("FORGE_TOKEN");
    if (!value) return {};
    std::string token(value);
    if (token.size() < 32 || token.size() > 256 ||
        !std::all_of(token.begin(), token.end(), [](unsigned char c) { return c >= 33 && c <= 126; }))
        throw std::runtime_error("FORGE_TOKEN must contain 32-256 printable non-space ASCII characters");
    return token;
}
bool token_matches(const std::string& expected, const std::string& supplied) {
    if (expected.size() != supplied.size()) return false;
    unsigned difference = 0;
    for (std::size_t i = 0; i < expected.size(); ++i)
        difference |= static_cast<unsigned char>(expected[i]) ^ static_cast<unsigned char>(supplied[i]);
    return difference == 0;
}
net::Connection connect_coordinator(std::uint16_t port, const std::string& host) {
    const auto token = configured_token();
    auto socket = net::connect(port, host, net::configured_tls());
    if (!token.empty()) {
        net::send(socket, {Type::authenticate, {token}});
        const auto response = net::receive(socket);
        if (response.type != Type::ok || !response.fields.empty())
            throw std::runtime_error("coordinator authentication failed");
    }
    return socket;
}
std::string target_architecture(const std::string& architecture) {
    if (!label(architecture))
        throw std::runtime_error("architecture must be native, any, or a 1-64 character architecture label");
    if (architecture == "any") return {};
    if (architecture != "native") return architecture;
    utsname system{};
    if (::uname(&system) != 0) throw std::runtime_error("architecture discovery failed");
    return system.machine;
}
Message assignment(const std::string& id, std::uint32_t timeout,
                   const std::vector<std::string>& arguments, const std::vector<InputFile>& files,
                   std::uint32_t attempt = 0, const Resources* limits = nullptr) {
    Message message{files.empty() ? Type::assignment : Type::staged_assignment, {id, std::to_string(timeout)}};
    if (attempt != 0) {
        message.type = files.empty() ? Type::attempt_assignment : Type::attempt_staged_assignment;
        message.fields.insert(message.fields.begin() + 1, std::to_string(attempt));
    }
    if (limits) {
        message.type = files.empty() ? Type::limited_assignment : Type::limited_staged_assignment;
        message.fields.insert(message.fields.end(), {std::to_string(limits->cpu), std::to_string(limits->memory_bytes)});
    }
    if (!files.empty()) append_inputs(message.fields, files);
    message.fields.insert(message.fields.end(), arguments.begin(), arguments.end());
    return message;
}
Message submission(const TaskRequest& task) {
    if (task.cpu == 0 || task.cpu > 1048576 || task.timeout == 0 || task.timeout > 30 || task.retries > 3 ||
        (task.enforce_limits && task.memory == 0) ||
        task.arguments.empty() || task.arguments.size() > 128 || task.arguments[0].empty())
        throw std::runtime_error("invalid job resources, timeout, or arguments");
    for (const auto& argument : task.arguments)
        if (argument.find('\0') != std::string::npos) throw std::runtime_error("NUL in argument");
    validate_inputs(task.files);
    Message request{task.files.empty() ? Type::submit : Type::staged_submit,
        {std::to_string(task.cpu), std::to_string(task.memory), std::to_string(task.timeout),
         "linux", target_architecture(task.architecture)}};
    if (task.retries != 0 || task.enforce_limits) {
        request.type = task.enforce_limits ? (task.files.empty() ? Type::limited_submit : Type::limited_staged_submit) :
            task.files.empty() ? Type::retry_submit : Type::retry_staged_submit;
        request.fields.push_back(std::to_string(task.retries));
    }
    if (!task.files.empty()) append_inputs(request.fields, task.files);
    request.fields.insert(request.fields.end(), task.arguments.begin(), task.arguments.end());
    net::encode(request);
    const Resources maximum{1048576, std::numeric_limits<std::uint64_t>::max()};
    net::encode(assignment("job-18446744073709551615", task.timeout, task.arguments, task.files, 4, &maximum));
    return request;
}
struct Attempt {
    std::uint32_t number;
    std::string worker;
    std::string state = "ASSIGNED";
    std::string exit_code = "-";
    Clock::time_point assigned_at{}, finished_at{};
    std::string diagnostic;
};
struct Job {
    std::string id;
    JobRequirements requirements;
    std::uint32_t timeout;
    std::vector<std::string> arguments;
    std::vector<InputFile> files;
    std::string state = "QUEUED";
    std::string worker;
    Clock::time_point assigned_at{};
    Clock::time_point queued_at{}, dispatch_started{}, dispatch_finished{}, completed_at{};
    std::optional<std::array<std::uint64_t, 4>> worker_phases;
    std::uint32_t retries = 0;
    bool enforce_limits = false;
    std::vector<Attempt> attempts;
    Message completion{Type::error, {"job unfinished"}};
    bool terminal = false;
    bool cancel_requested = false;
    Clock::time_point cancel_at{};
    std::string cancel_cleanup;
};
struct Record {
    std::vector<std::string> capabilities;
    Clock::time_point last_seen;
    bool connected = true;
    std::shared_ptr<Job> job;
    Resources reserved{0, 0};
    bool staging = false;
    bool long_poll = false;
    bool profiling = false;
    bool attempts = false;
    bool cancellable = false;
    bool cgroup_limits = false;
};
struct Registry {
    std::mutex mutex;
    std::map<std::string, Record> workers;
    std::map<std::string, std::shared_ptr<Job>> jobs;
    std::deque<std::shared_ptr<Job>> history;
    std::condition_variable changed;
    std::uint64_t next_job = 1;
};
void close_attempt(Job& job, const std::string& state, const std::string& diagnostic = {}) {
    if (job.attempts.empty() || job.attempts.back().finished_at != Clock::time_point{}) return;
    auto& attempt = job.attempts.back();
    attempt.state = state;
    attempt.finished_at = Clock::now();
    attempt.diagnostic = diagnostic;
    if (job.completion.type == Type::result) attempt.exit_code = job.completion.fields[1];
}
void complete_cancellation(Job& job, const std::string& cleanup, const Message* result = nullptr) {
    job.cancel_requested = true;
    job.cancel_cleanup = std::any_of(job.attempts.begin(), job.attempts.end(),
        [](const Attempt& attempt) { return attempt.state == "LOST"; }) ? "unconfirmed" : cleanup;
    job.terminal = true;
    job.state = "CANCELLED";
    job.completed_at = Clock::now();
    job.completion = {Type::cancelled_result, {job.id, "130", result ? result->fields[2] : "",
        result ? result->fields[3] : "", "0", result ? result->fields[5] : "0", job.cancel_cleanup}};
    close_attempt(job, "CANCELLED", "cancellation cleanup: " + cleanup);
    if (result && !job.attempts.empty()) job.attempts.back().exit_code = result->fields[1];
}
void finish(Registry& registry, Record& record, Message result) {
    if (!record.job) return;
    if (!record.job->terminal && record.job->cancel_requested) {
        complete_cancellation(*record.job, result.type == Type::result ? "reported" : "unconfirmed",
                              result.type == Type::result ? &result : nullptr);
    } else if (!record.job->terminal) {
        record.job->completion = std::move(result);
        record.job->terminal = true;
        record.job->completed_at = Clock::now();
        const auto& completion = record.job->completion;
        record.job->state = completion.type == Type::error ? (stopped ? "CANCELLED" : "LOST") :
            completion.fields[4] == "1" ? "TIMED_OUT" :
            completion.fields[1] == "0" ? "SUCCEEDED" : "FAILED";
        close_attempt(*record.job, record.job->state,
                      completion.type == Type::error ? completion.fields[0] : "");
    }
    record.job.reset();
    record.reserved = {0, 0};
    registry.changed.notify_all();
}
void worker_lost(Registry& registry, Record& record) {
    record.connected = false;
    if (!record.job) return;
    auto& job = *record.job;
    if (!stopped && !job.terminal && !job.cancel_requested && job.attempts.size() <= job.retries) {
        close_attempt(job, "LOST", "worker session ended before an accepted result; execution outcome unknown");
        job.state = "QUEUED";
        job.worker.clear();
        job.queued_at = Clock::now();
        job.assigned_at = job.dispatch_started = job.dispatch_finished = {};
        job.worker_phases.reset();
        record.job.reset();
        record.reserved = {0, 0};
        registry.changed.notify_all();
    } else {
        finish(registry, record, {Type::error, {job.id + (stopped ? ": coordinator stopping" :
            job.retries == 0 ? ": worker lost; not retried" : ": worker lost; retry budget exhausted")}});
    }
}
void schedule(Registry& registry) {
    // Caller holds the registry lock through selection AND reservation.
    for (auto& [id, job] : registry.jobs) {
        if (stopped) return;
        if (job->state != "QUEUED" || job->terminal || job->cancel_requested) continue;
        std::vector<Worker> candidates;
        for (const auto& [worker_id, record] : registry.workers) {
            const auto& c = record.capabilities;
            candidates.push_back({worker_id, c[1], c[2],
                {static_cast<std::uint32_t>(number(c[3])), number(c[4])}, record.reserved,
                c[5] == "1", record.connected && !record.job &&
                    (job->files.empty() || record.staging) &&
                    (job->retries == 0 || record.attempts) &&
                    (!job->enforce_limits || record.cgroup_limits) &&
                    Clock::now() - record.last_seen < std::chrono::seconds(1)});
        }
        auto selected = select_worker(candidates, job->requirements);
        if (!selected) continue;
        auto& record = registry.workers.at(candidates[*selected].id);
        job->worker = candidates[*selected].id;
        record.job = job;
        record.reserved = job->requirements.resources;
        job->state = "ASSIGNED";
        job->assigned_at = Clock::now();
        job->attempts.push_back({static_cast<std::uint32_t>(job->attempts.size() + 1), job->worker,
                                 "ASSIGNED", "-", job->assigned_at, {}, {}});
        registry.changed.notify_all();
    }
}
void submit(net::Connection& connection, Registry& registry, const Message& message) {
    const auto& fields = message.fields;
    if (fields.size() < 6 ||
        fields[3] != "linux" || (!fields[4].empty() && !label(fields[4])))
        throw std::runtime_error("invalid submission schema");
    auto cpu = number(fields[0]);
    auto memory = number(fields[1]);
    auto timeout = number(fields[2]);
    if (cpu == 0 || cpu > 1048576 || timeout == 0 || timeout > 30)
        throw std::runtime_error("invalid job resource request or timeout");
    std::size_t argument_offset = 5;
    auto job = std::make_shared<Job>();
    job->enforce_limits = message.type == Type::limited_submit || message.type == Type::limited_staged_submit;
    if (job->enforce_limits && memory == 0) throw std::runtime_error("enforced jobs require a positive memory byte limit");
    if (message.type == Type::retry_submit || message.type == Type::retry_staged_submit || job->enforce_limits) {
        const auto retries = number(fields[argument_offset++]);
        if ((!job->enforce_limits && retries == 0) || retries > 3) throw std::runtime_error("invalid retry count");
        job->retries = static_cast<std::uint32_t>(retries);
    }
    if (message.type == Type::staged_submit || message.type == Type::retry_staged_submit || message.type == Type::limited_staged_submit)
        job->files = parse_inputs(fields, argument_offset);
    if (argument_offset >= fields.size() || fields.size() - argument_offset > 128 || fields[argument_offset].empty())
        throw std::runtime_error("invalid submission arguments");
    for (std::size_t i = argument_offset; i < fields.size(); ++i)
        if (fields[i].find('\0') != std::string::npos) throw std::runtime_error("NUL in argument");
    job->requirements = {fields[3], fields[4], {static_cast<std::uint32_t>(cpu), memory}, false};
    job->timeout = static_cast<std::uint32_t>(timeout);
    job->arguments.assign(fields.begin() + static_cast<std::ptrdiff_t>(argument_offset), fields.end());
    const Resources maximum{1048576, std::numeric_limits<std::uint64_t>::max()};
    net::encode(assignment("job-18446744073709551615", job->timeout, job->arguments, job->files, 4, &maximum));
    {
        std::unique_lock lock(registry.mutex);
        if (registry.jobs.size() >= 128) throw std::runtime_error("job queue full");
        job->id = "job-" + std::to_string(registry.next_job++);
        job->queued_at = Clock::now();
        registry.jobs.emplace(job->id, job);
        schedule(registry);
        while (!job->terminal && !stopped) {
            registry.changed.wait_for(lock, std::chrono::milliseconds(100));
            if (job->state == "QUEUED" && Clock::now() - job->queued_at >= std::chrono::seconds(5)) {
                job->terminal = true;
                job->completed_at = Clock::now();
                job->state = "UNSCHEDULABLE";
                job->completion = {Type::error, {job->id + ": no available compatible worker within 5 seconds"}};
            }
        }
        if (!job->terminal) {
            if (job->cancel_requested) complete_cancellation(*job, "unconfirmed");
            else {
                job->state = "CANCELLED";
                job->completion = {Type::error, {job->id + ": coordinator stopping"}};
                job->terminal = true;
                job->completed_at = Clock::now();
                close_attempt(*job, "CANCELLED", "coordinator stopping");
            }
        }
        registry.jobs.erase(job->id);
        job->files.clear();
        registry.history.push_back(job);
        if (registry.history.size() > 64) registry.history.pop_front();
    }
    if ((job->retries != 0 || job->enforce_limits) && job->completion.type == Type::error)
        net::send(connection, {Type::job_error, {job->id, job->completion.fields[0]}});
    else net::send(connection, job->completion);
}
Message snapshot(Registry& registry, bool capabilities = false, bool retries = false, bool resources = false) {
    std::lock_guard lock(registry.mutex);
    Message result{resources ? Type::resource_snapshot : retries ? Type::retry_capability_snapshot :
        capabilities ? Type::capability_snapshot : Type::snapshot, {}};
    for (const auto& [id, record] : registry.workers) {
        const auto age = Clock::now() - record.last_seen;
        const std::string state = !record.connected || age >= std::chrono::seconds(2) ? "DEAD" :
            age >= std::chrono::seconds(1) ? "SUSPECT" : "READY";
        result.fields.insert(result.fields.end(), record.capabilities.begin(), record.capabilities.end());
        result.fields.push_back(capabilities && state == "READY" && record.job ? "BUSY" : state);
        if (capabilities) result.fields.push_back(record.staging ? "staging-v1" : "");
        if (retries) result.fields.push_back(record.attempts ? "attempt-v1" : "");
        if (resources) result.fields.push_back(record.cgroup_limits ? "cgroup2-v1" : "");
    }
    return result;
}
std::string summary(const std::vector<std::string>& arguments) {
    std::string text;
    for (const auto& arg : arguments) {
        if (!text.empty()) text += ' ';
        for (unsigned char c : arg) {
            if (text.size() >= 80) return text + "...";
            text += c >= 32 && c < 127 ? static_cast<char>(c) : '?';
        }
    }
    return text;
}
std::string interval(Clock::time_point begin, Clock::time_point end) {
    if (begin == Clock::time_point{} || end == Clock::time_point{} || end < begin) return {};
    return std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(end - begin).count());
}
Message timing_snapshot(const Job& job) {
    Message result{Type::timing_snapshot, {job.id, job.state,
        interval(job.queued_at, job.assigned_at), interval(job.assigned_at, job.dispatch_started),
        interval(job.dispatch_started, job.dispatch_finished), interval(job.assigned_at, job.completed_at)}};
    for (std::size_t i = 0; i < 4; ++i)
        result.fields.push_back(job.worker_phases ? std::to_string((*job.worker_phases)[i]) : "");
    return result;
}
Message attempt_snapshot(const Job& job) {
    Message result{Type::attempt_snapshot, {job.id, job.state, std::to_string(job.retries + 1)}};
    for (const auto& attempt : job.attempts)
        result.fields.insert(result.fields.end(), {std::to_string(attempt.number), attempt.worker,
            attempt.state, attempt.exit_code, interval(attempt.assigned_at, attempt.finished_at), attempt.diagnostic});
    return result;
}
Message inspect_job(const Job& job, Type request) {
    if (request == Type::timings) return timing_snapshot(job);
    if (request == Type::attempts) return attempt_snapshot(job);
    if (!job.terminal) return {Type::error, {job.id + ": not finished; live streaming is not implemented"}};
    return job.completion;
}
Message inspect(Registry& registry, const Message& request) {
    std::lock_guard lock(registry.mutex);
    if (request.type == Type::jobs && request.fields.empty()) {
        Message reply{Type::job_list, {}};
        auto append = [&reply](const std::shared_ptr<Job>& job) {
            reply.fields.insert(reply.fields.end(), {job->id, job->state,
                job->worker.empty() ? "-" : job->worker,
                job->terminal && (job->completion.type == Type::result || job->completion.type == Type::cancelled_result)
                    ? job->completion.fields[1] : "-",
                summary(job->arguments)});
        };
        for (const auto& [id, job] : registry.jobs) append(job);
        for (const auto& job : registry.history) append(job);
        return reply;
    }
    if ((request.type != Type::logs && request.type != Type::timings && request.type != Type::attempts) ||
        request.fields.size() != 1 || !label(request.fields[0]))
        throw std::runtime_error("invalid inspection request");
    const auto& id = request.fields[0];
    auto active = registry.jobs.find(id);
    if (active != registry.jobs.end()) return inspect_job(*active->second, request.type);
    for (const auto& job : registry.history)
        if (job->id == id) return inspect_job(*job, request.type);
    return {Type::error, {id + ": unknown or evicted job (history is in memory only)"}};
}
Message request_cancellation(Registry& registry, const Message& request) {
    if (request.fields.size() != 1 || !label(request.fields[0]))
        throw std::runtime_error("cancel requires one job ID");
    std::lock_guard lock(registry.mutex);
    const auto& id = request.fields[0];
    std::shared_ptr<Job> job;
    const auto active = registry.jobs.find(id);
    if (active != registry.jobs.end()) job = active->second;
    else for (const auto& retained : registry.history)
        if (retained->id == id) { job = retained; break; }
    if (!job) return {Type::error, {id + ": unknown or evicted job (history is in memory only)"}};
    if (!job->terminal && !job->cancel_requested) {
        if (job->state == "QUEUED") complete_cancellation(*job, "not-started");
        else {
            auto& record = registry.workers.at(job->worker);
            if (record.job != job) throw std::runtime_error("job reservation mismatch");
            if (job->state == "ASSIGNED") {
                complete_cancellation(*job, "not-started");
                record.job.reset();
                record.reserved = {0, 0};
            } else {
                if (!record.cancellable)
                    return {Type::error, {id + ": running worker does not support cancel-v1; upgrade the worker"}};
                job->cancel_requested = true;
                job->cancel_at = Clock::now();
                job->cancel_cleanup = "pending";
                job->state = "CANCELLING";
                job->attempts.back().state = "CANCELLING";
            }
        }
        registry.changed.notify_all();
        schedule(registry);
    }
    return {Type::cancellation_status, {job->id, job->state,
        job->cancel_cleanup.empty() ? "not-requested" : job->cancel_cleanup}};
}
void handle(net::Socket peer, Registry& registry, const std::string& token, const net::TlsContext& tls) {
    std::string registered;
    net::Connection socket(std::move(peer));
    bool ready = false;
    try {
        socket.secure(tls, true);
        ready = true;
        auto message = net::receive(socket);
        if (!token.empty()) {
            if (message.type != Type::authenticate || message.fields.size() != 1 ||
                !token_matches(token, message.fields[0]))
                throw std::runtime_error("authentication required or invalid credential");
            net::send(socket, {Type::ok, {}});
            message = net::receive(socket);
        }
        if (message.type == Type::cluster && message.fields.empty()) {
            net::send(socket, snapshot(registry));
            return;
        }
        if ((message.type == Type::worker_capabilities || message.type == Type::retry_capabilities ||
             message.type == Type::resource_capabilities) && message.fields.empty()) {
            net::send(socket, snapshot(registry, true, message.type != Type::worker_capabilities,
                                       message.type == Type::resource_capabilities));
            return;
        }
        if (message.type == Type::jobs || message.type == Type::logs || message.type == Type::timings || message.type == Type::attempts) {
            net::send(socket, inspect(registry, message));
            return;
        }
        if (message.type == Type::cancel) {
            net::send(socket, request_cancellation(registry, message));
            return;
        }
        if (message.type == Type::submit || message.type == Type::staged_submit ||
            message.type == Type::retry_submit || message.type == Type::retry_staged_submit ||
            message.type == Type::limited_submit || message.type == Type::limited_staged_submit) {
            submit(socket, registry, message);
            return;
        }
        if (message.type != Type::register_worker ||
            message.fields.size() < 6 || message.fields.size() > 12)
            throw std::runtime_error("expected registration or cluster request");
        const auto& f = message.fields;
        if (!label(f[0]) || !label(f[1]) || !label(f[2]) ||
            number(f[3]) == 0 || number(f[3]) > 1048576 || number(f[4]) == 0 ||
            (f[5] != "0" && f[5] != "1") || (f.size() >= 7 && f[6] != "staging-v1") ||
            (f.size() >= 8 && f[7] != "longpoll-v1") || (f.size() >= 9 && f[8] != "timing-v1") ||
            (f.size() >= 10 && f[9] != "attempt-v1") || (f.size() >= 11 && f[10] != "cancel-v1") ||
            (f.size() == 12 && f[11] != "cgroup2-v1"))
            throw std::runtime_error("invalid worker capabilities");
        {
            std::lock_guard lock(registry.mutex);
            auto existing = registry.workers.find(f[0]);
            // Do not replace a live session: its cleanup could otherwise mark
            // the replacement disconnected. Reuse IDs only after session close.
            if (existing != registry.workers.end() && existing->second.connected)
                throw std::runtime_error("worker ID already connected");
            if (existing == registry.workers.end() && registry.workers.size() >= 64)
                throw std::runtime_error("worker registry full");
            registry.workers.insert_or_assign(f[0], Record{
                std::vector<std::string>(f.begin(), f.begin() + 6), Clock::now(), true, nullptr, {0, 0},
                f.size() >= 7, f.size() >= 8, f.size() >= 9, f.size() >= 10, f.size() >= 11, f.size() == 12});
            registered = f[0];
        }
        net::send(socket, {Type::ok, {}});
        while (!stopped) {
            message = net::receive(socket, std::chrono::seconds(2));
            Message reply{Type::ok, {}};
            std::shared_ptr<Job> dispatched;
            {
                std::unique_lock lock(registry.mutex);
                auto& record = registry.workers.at(registered);
                if (record.job && record.job->cancel_requested &&
                    Clock::now() - record.job->cancel_at > std::chrono::seconds(5))
                    throw std::runtime_error("cancellation acknowledgement deadline exceeded");
                if (record.job && Clock::now() - record.job->assigned_at >
                    std::chrono::seconds(record.job->timeout + 5))
                    throw std::runtime_error("job result deadline exceeded");
                if (message.type == Type::result || message.type == Type::profiled_result || message.type == Type::attempt_result) {
                    if (record.attempts != (message.type == Type::attempt_result))
                        throw std::runtime_error("result attempt capability mismatch");
                    if (message.type == Type::attempt_result) {
                        if (!record.job || message.fields.size() != 11 ||
                            number(message.fields[1]) != record.job->attempts.back().number)
                            throw std::runtime_error("invalid or stale execution attempt");
                        message.fields.erase(message.fields.begin() + 1);
                        message.type = Type::profiled_result;
                    }
                    const auto& r = message.fields;
                    const bool profiled = message.type == Type::profiled_result;
                    if (!record.job || (record.job->state != "RUNNING" && record.job->state != "CANCELLING") ||
                        record.profiling != profiled ||
                        r.size() != (profiled ? 10u : 6u) ||
                        r[0] != record.job->id || number(r[1]) > 255 ||
                        r[2].size() > 16384 || r[3].size() > 16384 ||
                        (r[4] != "0" && r[4] != "1") || (r[5] != "0" && r[5] != "1"))
                        throw std::runtime_error("invalid or stale job result");
                    if (profiled) {
                        std::array<std::uint64_t, 4> phases;
                        for (std::size_t i = 0; i < phases.size(); ++i) {
                            phases[i] = number(r[6 + i]);
                            if (phases[i] > 60000000) throw std::runtime_error("invalid worker timing interval");
                        }
                        record.job->worker_phases = phases;
                    }
                    finish(registry, record, {Type::result, std::vector<std::string>(r.begin(), r.begin() + 6)});
                } else if (message.type != Type::heartbeat || !message.fields.empty()) {
                    throw std::runtime_error("expected heartbeat or result");
                }
                record.last_seen = Clock::now();
                schedule(registry);
                if (message.type == Type::heartbeat && record.long_poll && !record.job)
                    registry.changed.wait_for(lock, std::chrono::milliseconds(500), [&] { return stopped || record.job; });
                if (!stopped && message.type == Type::heartbeat && record.job && record.job->state == "ASSIGNED") {
                    auto& job = *record.job;
                    reply = assignment(job.id, job.timeout, job.arguments, job.files,
                                       record.attempts ? job.attempts.back().number : 0,
                                       record.cgroup_limits ? &job.requirements.resources : nullptr);
                    job.state = "RUNNING";
                    job.attempts.back().state = "RUNNING";
                    dispatched = record.job;
                } else if (!stopped && message.type == Type::heartbeat && record.job && record.job->cancel_requested) {
                    reply = {Type::stop_attempt, {record.job->id, std::to_string(record.job->attempts.back().number)}};
                }
            }
            const auto sent_at = Clock::now();
            net::send(socket, reply);
            const auto sent_until = Clock::now();
            if (dispatched) {
                std::lock_guard lock(registry.mutex);
                dispatched->dispatch_started = sent_at;
                dispatched->dispatch_finished = sent_until;
            }
        }
    } catch (const std::exception& error) {
        try { if (ready) net::send(socket, {Type::error, {error.what()}}, std::chrono::milliseconds(100)); }
        catch (...) {} // A disconnected client must not terminate the server.
    }
    if (!registered.empty()) {
        std::lock_guard lock(registry.mutex);
        auto& record = registry.workers.at(registered);
        worker_lost(registry, record);
        schedule(registry);
    }
}
void expect_ok(const Message& message) {
    if (message.type == Type::error && message.fields.size() == 1)
        throw std::runtime_error(message.fields.front());
    if (message.type != Type::ok || !message.fields.empty())
        throw std::runtime_error("unexpected coordinator response");
}
struct CompletionNotice {
    std::mutex mutex;
    std::condition_variable changed;
    bool ready = false;
    void signal() {
        { std::lock_guard lock(mutex); ready = true; }
        changed.notify_one();
    }
};
struct WorkerOutcome {
    ExecutionResult result;
    std::array<std::uint64_t, 3> phases{};
    Clock::time_point completed_at{};
};
std::uint64_t microseconds(Clock::time_point begin, Clock::time_point end) {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(end - begin).count());
}
WorkerOutcome perform(const std::vector<std::string>& arguments, const std::vector<InputFile>& files,
                      std::uint32_t timeout, const std::atomic<bool>& cancel, Clock::time_point received,
                      const CgroupRoot* root, Resources limits) {
    WorkerOutcome outcome;
    std::optional<Workspace> workspace;
    std::optional<CgroupJob> group;
    Clock::time_point execution_started{};
    try {
        if (root) group.emplace(*root, limits);
        if (!files.empty()) workspace.emplace(files);
        execution_started = Clock::now();
        outcome.phases[0] = microseconds(received, execution_started);
        outcome.result = execute(arguments, timeout, cancel, workspace ? workspace->path() : "", group ? &*group : nullptr);
        outcome.phases[1] = microseconds(execution_started, Clock::now());
        if (group && group->oom_killed()) {
            const std::string diagnostic = "\n[forge] job cgroup reports an OOM kill\n";
            const auto room = 16384 - outcome.result.error.size();
            outcome.result.error.append(diagnostic, 0, room);
            if (diagnostic.size() > room) outcome.result.truncated = true;
        }
    } catch (const ResourceCleanupError&) {
        throw;
    } catch (const std::exception& error) {
        const auto failed_at = Clock::now();
        if (execution_started == Clock::time_point{}) outcome.phases[0] = microseconds(received, failed_at);
        else outcome.phases[1] = microseconds(execution_started, failed_at);
        outcome.result.exit_code = 125;
        outcome.result.error = std::string("worker execution setup failed: ") + error.what();
    }
    const auto cleanup_started = Clock::now();
    if (group) group->cleanup();
    workspace.reset();
    outcome.completed_at = Clock::now();
    outcome.phases[2] = microseconds(cleanup_started, outcome.completed_at);
    return outcome;
}
}

int coordinator(std::uint16_t port, const std::string& bind) {
    const auto token = configured_token();
    install_signals();
    const auto tls = net::configured_tls();
    auto listener = net::listen(port, bind, tls);
    Registry registry;
    struct Slot { std::atomic<bool> done{true}; std::jthread thread; };
    // Fixed slots bound threads and retained thread objects. Destruction joins
    // every handler before registry destruction.
    std::array<Slot, 96> slots;
    std::cout << "Coordinator listening on " << bind << ":" << port << (tls ? " (mutual TLS)" : " (plaintext loopback)") << std::endl;
    while (!stopped) {
        pollfd p{listener.get(), POLLIN, 0};
        const int ready = ::poll(&p, 1, 100);
        if (ready < 0) {
            if (errno == EINTR) continue;
            stopped = true;
            throw std::runtime_error("accept poll failed");
        }
        if (ready == 0) continue;
        net::Socket peer(::accept4(listener.get(), nullptr, nullptr, SOCK_CLOEXEC | SOCK_NONBLOCK));
        if (peer.get() < 0) continue;
        auto slot = std::find_if(slots.begin(), slots.end(), [](const Slot& s) { return s.done.load(); });
        if (slot == slots.end()) continue;
        if (slot->thread.joinable()) slot->thread.join();
        slot->done = false;
        try {
            slot->thread = std::jthread([socket = std::move(peer), &registry, &token, &tls, &done = slot->done]() mutable {
                handle(std::move(socket), registry, token, tls);
                done = true;
            });
        } catch (...) {
            stopped = true;
            throw;
        }
    }
    return 0;
}

int join(std::uint16_t port, const std::string& id, const std::string& host, const std::string& cgroup_root) {
    if (!label(id)) throw std::runtime_error("worker ID must be 1-64 letters, digits, dots, underscores or hyphens");
    install_signals();
    utsname system{};
    std::optional<CgroupRoot> resource_root;
    if (!cgroup_root.empty()) resource_root.emplace(cgroup_root);
    const auto capacity = resource_capacity(cgroup_root);
    if (::uname(&system) != 0)
        throw std::runtime_error("capability discovery failed");
    auto socket = connect_coordinator(port, host);
    Message registration{Type::register_worker, {id, "linux", system.machine, std::to_string(capacity.cpu),
        std::to_string(capacity.memory_bytes), "0", "staging-v1", "longpoll-v1", "timing-v1", "attempt-v1", "cancel-v1"}};
    if (resource_root) registration.fields.push_back("cgroup2-v1");
    net::send(socket, registration);
    expect_ok(net::receive(socket));
    std::cout << "Worker " << id << " registered" << std::endl;
    std::atomic<bool> cancel{false};
    CompletionNotice completed;
    std::future<WorkerOutcome> execution;
    // Cancel before the future destructor waits during disconnect or shutdown.
    struct CancelOnExit { std::atomic<bool>& flag; ~CancelOnExit() { flag = true; } } cleanup{cancel};
    std::string job_id;
    std::uint32_t attempt = 0;
    auto next_heartbeat = Clock::now();
    while (!stopped) {
        bool finished = false;
        if (execution.valid()) {
            std::unique_lock lock(completed.mutex);
            completed.changed.wait_until(lock, std::min(next_heartbeat, Clock::now() + std::chrono::milliseconds(100)),
                                        [&] { return completed.ready || stopped; });
            finished = completed.ready;
        }
        if (stopped) break;
        if (finished) {
            WorkerOutcome outcome;
            try { outcome = execution.get(); }
            catch (const ResourceCleanupError&) { throw; }
            catch (const std::exception& error) {
                outcome.result.exit_code = 125;
                outcome.result.error = std::string("worker execution setup failed: ") + error.what();
                outcome.completed_at = Clock::now();
            }
            const auto& result = outcome.result;
            Message response{Type::profiled_result, {job_id, std::to_string(result.exit_code),
                result.output, result.error, result.timed_out ? "1" : "0", result.truncated ? "1" : "0"}};
            for (const auto phase : outcome.phases) response.fields.push_back(std::to_string(phase));
            response.fields.push_back(std::to_string(microseconds(outcome.completed_at, Clock::now())));
            response.type = Type::attempt_result;
            response.fields.insert(response.fields.begin() + 1, std::to_string(attempt));
            net::send(socket, response);
            expect_ok(net::receive(socket));
            job_id.clear();
            continue;
        }
        if (execution.valid() && Clock::now() < next_heartbeat) continue;
        net::send(socket, {Type::heartbeat, {}});
        auto response = net::receive(socket);
        const auto received_at = Clock::now();
        next_heartbeat = received_at + std::chrono::milliseconds(500);
        if (response.type == Type::stop_attempt) {
            if (!execution.valid() || response.fields.size() != 2 || response.fields[0] != job_id ||
                number(response.fields[1]) != attempt)
                throw std::runtime_error("invalid cancellation attempt");
            cancel = true;
        } else if (response.type == Type::attempt_assignment || response.type == Type::attempt_staged_assignment ||
                   response.type == Type::limited_assignment || response.type == Type::limited_staged_assignment) {
            const bool limited = response.type == Type::limited_assignment || response.type == Type::limited_staged_assignment;
            if (limited != resource_root.has_value()) throw std::runtime_error("assignment resource enforcement capability mismatch");
            if (response.fields.size() < 4 || number(response.fields[1]) == 0 || number(response.fields[1]) > 4)
                throw std::runtime_error("invalid execution attempt");
            attempt = static_cast<std::uint32_t>(number(response.fields[1]));
            response.fields.erase(response.fields.begin() + 1);
            response.type = response.type == Type::attempt_assignment || response.type == Type::limited_assignment ?
                Type::assignment : Type::staged_assignment;
            if (execution.valid() || response.fields.size() < 3 ||
                !label(response.fields[0])) throw std::runtime_error("invalid assignment");
            const auto timeout = number(response.fields[1]);
            if (timeout == 0 || timeout > 30) throw std::runtime_error("invalid execution timeout");
            job_id = response.fields[0];
            std::size_t offset = 2;
            Resources limits;
            if (limited) {
                if (response.fields.size() < 5) throw std::runtime_error("missing assignment limits");
                const auto cpu = number(response.fields[offset++]);
                limits.memory_bytes = number(response.fields[offset++]);
                if (cpu == 0 || cpu > capacity.cpu || limits.memory_bytes > capacity.memory_bytes)
                    throw std::runtime_error("assignment exceeds worker capacity");
                limits.cpu = static_cast<std::uint32_t>(cpu);
            }
            std::vector<InputFile> files;
            if (response.type == Type::staged_assignment) files = parse_inputs(response.fields, offset);
            if (offset >= response.fields.size() || response.fields.size() - offset > 128 || response.fields[offset].empty())
                throw std::runtime_error("invalid assignment arguments");
            std::vector<std::string> arguments(response.fields.begin() + static_cast<std::ptrdiff_t>(offset), response.fields.end());
            cancel = false;
            { std::lock_guard lock(completed.mutex); completed.ready = false; }
            execution = std::async(std::launch::async, [arguments = std::move(arguments), files = std::move(files), timeout,
                                                     received_at, limits, root = resource_root ? &*resource_root : nullptr, &cancel, &completed] {
                struct Notify { CompletionNotice& notice; ~Notify() { notice.signal(); } } notify{completed};
                return perform(arguments, files, static_cast<std::uint32_t>(timeout), cancel, received_at, root, limits);
            });
        } else expect_ok(response);
    }
    return 0;
}

int cluster(std::uint16_t port, const std::string& host, bool details) {
    auto socket = connect_coordinator(port, host);
    net::send(socket, {details ? Type::resource_capabilities : Type::cluster, {}});
    auto result = net::receive(socket);
    const auto width = details ? 10u : 7u;
    if (result.type != (details ? Type::resource_snapshot : Type::snapshot) || result.fields.size() % width != 0)
        throw std::runtime_error("invalid cluster snapshot");
    std::cout << "ID\tOS\tARCH\tCPU\tRAM_BYTES\tCUDA\tSTATE" << (details ? "\tSTAGING\tATTEMPTS\tLIMITS\n" : "\n");
    for (std::size_t i = 0; i < result.fields.size(); ++i)
        std::cout << result.fields[i] << ((i + 1) % width == 0 ? '\n' : '\t');
    return 0;
}

int ps(std::uint16_t port, const std::string& host) {
    auto socket = connect_coordinator(port, host);
    net::send(socket, {Type::jobs, {}});
    auto response = net::receive(socket);
    if (response.type != Type::job_list || response.fields.size() % 5 != 0)
        throw std::runtime_error("invalid job list");
    std::cout << "ID\tSTATE\tWORKER\tEXIT\tCOMMAND\n";
    for (std::size_t i = 0; i < response.fields.size(); ++i)
        std::cout << response.fields[i] << ((i + 1) % 5 == 0 ? '\n' : '\t');
    return 0;
}
int logs(std::uint16_t port, const std::string& id, const std::string& host) {
    auto socket = connect_coordinator(port, host);
    net::send(socket, {Type::logs, {id}});
    auto response = net::receive(socket);
    if (response.type == Type::error && response.fields.size() == 1)
        throw std::runtime_error(response.fields[0]);
    const bool cancelled = response.type == Type::cancelled_result;
    if ((response.type != Type::result && !cancelled) || response.fields.size() != (cancelled ? 7u : 6u) ||
        (cancelled && !cancellation_cleanup(response.fields[6])))
        throw std::runtime_error("invalid retained result");
    std::cout << response.fields[2];
    std::cerr << response.fields[3];
    std::cerr << "[forge] " << response.fields[0] << " exit=" << response.fields[1] << '\n';
    if (cancelled) std::cerr << "[forge] CANCELLED cleanup=" << response.fields[6] << '\n';
    if (response.fields[4] == "1") std::cerr << "[forge] execution timed out\n";
    if (response.fields[5] == "1") std::cerr << "[forge] output truncated at 16384 bytes per stream\n";
    return 0; // retrieval succeeded, independent of the original command's exit
}
int timings(std::uint16_t port, const std::string& id, const std::string& host) {
    auto socket = connect_coordinator(port, host);
    net::send(socket, {Type::timings, {id}});
    const auto response = net::receive(socket);
    const auto& fields = response.fields;
    if (response.type == Type::error && fields.size() == 1) throw std::runtime_error(fields[0]);
    if (response.type != Type::timing_snapshot || fields.size() != 10 || !label(fields[0]) || !label(fields[1]))
        throw std::runtime_error("invalid timing snapshot");
    constexpr std::array<const char*, 8> names{"queue_seconds", "dispatch_wait_seconds", "dispatch_write_seconds",
        "assigned_to_terminal_seconds", "worker_prepare_seconds", "worker_execute_seconds", "worker_cleanup_seconds",
        "worker_result_wait_seconds"};
    std::string json = "{\"job_id\":\"" + fields[0] + "\",\"state\":\"" + fields[1] + "\"";
    for (std::size_t i = 0; i < names.size(); ++i)
        json += ",\"" + std::string(names[i]) + "\":" + (fields[i + 2].empty() ? "null" :
            std::to_string(static_cast<double>(number(fields[i + 2])) / 1000000.0));
    std::cout << json << "}\n";
    return 0;
}
int attempts(std::uint16_t port, const std::string& id, const std::string& host) {
    auto socket = connect_coordinator(port, host);
    net::send(socket, {Type::attempts, {id}});
    const auto response = net::receive(socket);
    const auto& fields = response.fields;
    if (response.type == Type::error && fields.size() == 1) throw std::runtime_error(fields[0]);
    if (response.type != Type::attempt_snapshot || fields.size() < 3 || (fields.size() - 3) % 6 != 0 ||
        fields[0] != id || !label(fields[0]) || !label(fields[1]) ||
        number(fields[2]) == 0 || number(fields[2]) > 4 || (fields.size() - 3) / 6 > number(fields[2]))
        throw std::runtime_error("invalid attempt snapshot");
    std::cerr << "[forge] " << fields[0] << " state=" << fields[1] << " attempts="
              << (fields.size() - 3) / 6 << '/' << fields[2] << '\n';
    std::cout << "ATTEMPT\tWORKER\tSTATE\tEXIT\tELAPSED_SECONDS\tDIAGNOSTIC\n";
    for (std::size_t i = 3; i < fields.size(); i += 6) {
        if (number(fields[i]) != (i - 3) / 6 + 1 || !label(fields[i + 1]) || !label(fields[i + 2]))
            throw std::runtime_error("invalid attempt record");
        std::cout << fields[i] << '\t' << fields[i + 1] << '\t' << fields[i + 2] << '\t'
                  << fields[i + 3] << '\t' << (fields[i + 4].empty() ? "-" :
                      std::to_string(static_cast<double>(number(fields[i + 4])) / 1000000.0)) << '\t'
                  << fields[i + 5] << '\n';
    }
    return 0;
}
int cancel(std::uint16_t port, const std::string& id, const std::string& host) {
    auto socket = connect_coordinator(port, host);
    net::send(socket, {Type::cancel, {id}});
    const auto response = net::receive(socket);
    const auto& fields = response.fields;
    if (response.type == Type::error && fields.size() == 1) throw std::runtime_error(fields[0]);
    if (response.type != Type::cancellation_status || fields.size() != 3 || fields[0] != id ||
        !label(fields[0]) || !label(fields[1]) ||
        (!cancellation_cleanup(fields[2]) && fields[2] != "pending" && fields[2] != "not-requested"))
        throw std::runtime_error("invalid cancellation status");
    std::cerr << "[forge] " << fields[0] << ' ' << fields[1] << " cleanup=" << fields[2] << '\n';
    return fields[1] == "CANCELLED" || fields[1] == "CANCELLING" ? 0 : 1;
}

void validate_task(const TaskRequest& task) {
    submission(task);
}
TaskResult submit_task(std::uint16_t port, const std::string& host, const TaskRequest& task) {
    const auto request = submission(task);
    auto socket = connect_coordinator(port, host);
    net::send(socket, request);
    const auto result = net::receive(socket, std::chrono::seconds(45 * (task.retries + 1)));
    if (result.type == Type::error && result.fields.size() == 1)
        return {{}, 1, {}, result.fields[0], false, false, true};
    if ((task.retries != 0 || task.enforce_limits) && result.type == Type::job_error && result.fields.size() == 2 && label(result.fields[0]))
        return {result.fields[0], 1, {}, result.fields[1], false, false, true};
    const auto& fields = result.fields;
    if (result.type == Type::cancelled_result && fields.size() == 7 && label(fields[0]) &&
        fields[1] == "130" && fields[2].size() <= 16384 && fields[3].size() <= 16384 && fields[4] == "0" &&
        (fields[5] == "0" || fields[5] == "1") && cancellation_cleanup(fields[6]))
        return {fields[0], 130, fields[2], fields[3], false, fields[5] == "1", false, true, fields[6]};
    if (result.type != Type::result || fields.size() != 6 || !label(fields[0]) || number(fields[1]) > 255 ||
        fields[2].size() > 16384 || fields[3].size() > 16384 ||
        (fields[4] != "0" && fields[4] != "1") || (fields[5] != "0" && fields[5] != "1"))
        throw std::runtime_error("invalid job result");
    return {fields[0], static_cast<int>(number(fields[1])), fields[2], fields[3],
            fields[4] == "1", fields[5] == "1", false};
}
std::size_t staging_worker_count(std::uint16_t port, const std::string& host, const TaskRequest& task) {
    const auto architecture = target_architecture(task.architecture);
    auto socket = connect_coordinator(port, host);
    const bool retries = task.retries != 0;
    const bool limits = task.enforce_limits;
    net::send(socket, {limits ? Type::resource_capabilities : retries ? Type::retry_capabilities : Type::worker_capabilities, {}});
    const auto response = net::receive(socket);
    const std::size_t width = limits ? 10 : retries ? 9 : 8;
    if (response.type != (limits ? Type::resource_snapshot : retries ? Type::retry_capability_snapshot : Type::capability_snapshot) ||
        response.fields.size() % width != 0)
        throw std::runtime_error("coordinator does not support requested worker capability discovery");
    std::size_t count = 0;
    const auto& f = response.fields;
    for (std::size_t i = 0; i < f.size(); i += width) {
        const auto cpu = number(f[i + 3]);
        if (cpu == 0 || cpu > 1048576) throw std::runtime_error("invalid worker CPU capacity");
        Worker worker{f[i], f[i + 1], f[i + 2],
            {static_cast<std::uint32_t>(cpu), number(f[i + 4])}, {0, 0}, f[i + 5] == "1",
            f[i + 6] == "READY" && f[i + 7] == "staging-v1" && (!retries || f[i + 8] == "attempt-v1") &&
            (!limits || f[i + 9] == "cgroup2-v1")};
        if (!rejection(worker, {"linux", architecture, {task.cpu, task.memory}, false})) ++count;
    }
    return count;
}
int run(std::uint16_t port, const std::string& host, const TaskRequest& task) {
    const auto result = submit_task(port, host, task);
    if (result.infrastructure_error) throw std::runtime_error(result.error);
    std::cerr << "[forge] " << result.job_id << " exit=" << result.exit_code << '\n';
    std::cout << result.output;
    std::cerr << result.error;
    if (result.cancelled) std::cerr << "[forge] CANCELLED cleanup=" << result.cancellation_cleanup << '\n';
    if (result.timed_out) std::cerr << "[forge] execution timed out\n";
    if (result.truncated) std::cerr << "[forge] output truncated at 16384 bytes per stream\n";
    return result.exit_code;
}
}
