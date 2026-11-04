#include "forge/runtime.hpp"
#include "forge/network.hpp"
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <fcntl.h>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <syncstream>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>

namespace forge {
namespace {
std::string quote(const std::string& value) {
    const char hex[] = "0123456789abcdef";
    std::string result = "\"";
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') { result += '\\'; result += static_cast<char>(c); }
        else if (c < 32 || c >= 127) {
            result += "\\u00";
            result += hex[c >> 4];
            result += hex[c & 15];
        } else result += static_cast<char>(c);
    }
    return result + '"';
}
std::string prefix(std::size_t index) {
    std::ostringstream text;
    text << std::setfill('0') << std::setw(4) << index + 1;
    return text.str();
}
void save(int directory, const std::string& name, const std::string& data) {
    net::Socket file(::openat(directory, name.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600));
    if (file.get() < 0) throw std::runtime_error("cannot create batch result: " + name);
    std::size_t offset = 0;
    while (offset < data.size()) {
        const auto count = ::write(file.get(), data.data() + offset, data.size() - offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) throw std::runtime_error("cannot write batch result: " + name);
        offset += static_cast<std::size_t>(count);
    }
}
struct Entry {
    TaskRequest task;
    std::string input;
    TaskResult result;
    bool submitted = false;
    bool stored = false;
    double elapsed_seconds = 0;
};
std::string state(const Entry& entry) {
    if (!entry.submitted) return "NOT_SUBMITTED";
    if (entry.result.infrastructure_error) return "ERROR";
    if (entry.result.timed_out) return "TIMED_OUT";
    return entry.result.exit_code == 0 ? "SUCCEEDED" : "FAILED";
}
std::string record(std::size_t index, const Entry& entry) {
    const auto& result = entry.result;
    return "{\"index\":" + std::to_string(index + 1) + ",\"input\":" + quote(entry.input) +
        ",\"job_id\":" + quote(result.job_id) + ",\"state\":" + quote(state(entry)) +
        ",\"exit_code\":" + (result.infrastructure_error ? "null" : std::to_string(result.exit_code)) +
        ",\"timed_out\":" + (result.timed_out ? "true" : "false") +
        ",\"truncated\":" + (result.truncated ? "true" : "false") +
        ",\"storage_complete\":" + (entry.stored ? "true" : "false") +
        ",\"elapsed_seconds\":" + (entry.submitted ? std::to_string(entry.elapsed_seconds) : "null") +
        ",\"stdout\":" + (entry.submitted ? quote(prefix(index) + ".stdout") : "null") +
        ",\"stderr\":" + (entry.submitted ? quote(prefix(index) + ".stderr") : "null") +
        ",\"diagnostic\":" + quote(result.infrastructure_error ? result.error : "") + "}";
}
}

int map(std::uint16_t port, const std::string& host, const TaskRequest& base,
        const std::vector<std::string>& inputs, std::size_t concurrency, const std::string& output) {
    if (inputs.empty() || inputs.size() > 256 || concurrency == 0 || concurrency > 16 || output.empty())
        throw std::runtime_error("map requires 1-256 input files, 1-16 parallel jobs, and an output directory");
    if (std::find(base.arguments.begin(), base.arguments.end(), "{}") == base.arguments.end())
        throw std::runtime_error("map command requires a standalone {} input placeholder");
    std::vector<Entry> entries;
    for (const auto& path : inputs) {
        Entry entry;
        entry.task = base;
        auto file = read_inputs({path});
        entry.input = file.front().name;
        entry.task.files.push_back(std::move(file.front()));
        for (auto& argument : entry.task.arguments)
            if (argument == "{}") argument = "./" + entry.input;
        validate_task(entry.task);
        entries.push_back(std::move(entry));
    }
    const auto workers = staging_worker_count(port, host, base);
    if (workers == 0) throw std::runtime_error(base.retries == 0 ?
        "no compatible idle READY workers advertise staging-v1" :
        "no compatible idle READY workers advertise staging-v1 and attempt-v1");
    concurrency = std::min({concurrency, workers, entries.size()});
    if (::mkdir(output.c_str(), 0700) != 0)
        throw std::runtime_error("cannot create output directory; it must not already exist and its parent must exist");
    net::Socket directory(::open(output.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (directory.get() < 0) throw std::runtime_error("cannot open batch output directory");
    std::atomic<std::size_t> next{0};
    std::atomic<bool> storage_failed{false};
    {
        std::vector<std::jthread> threads;
        for (std::size_t worker = 0; worker < concurrency; ++worker) {
            threads.emplace_back([&] {
                while (!storage_failed.load()) {
                    const auto index = next.fetch_add(1);
                    if (index >= entries.size()) break;
                    auto& entry = entries[index];
                    entry.submitted = true;
                    const auto started = std::chrono::steady_clock::now();
                    try { entry.result = submit_task(port, host, entry.task); }
                    catch (const std::exception& error) {
                        entry.result.infrastructure_error = true;
                        entry.result.error = std::string("submission outcome may be unknown; not retried: ") + error.what();
                    }
                    entry.elapsed_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
                    entry.task.files.clear();
                    try {
                        save(directory.get(), prefix(index) + ".stdout", entry.result.output);
                        save(directory.get(), prefix(index) + ".stderr", entry.result.error);
                        entry.stored = true;
                        save(directory.get(), prefix(index) + ".json", record(index, entry) + '\n');
                        std::osyncstream(std::cerr) << "[forge] task " << index + 1 << '/' << entries.size()
                            << ' ' << state(entry) << ' ' << entry.result.job_id << '\n';
                    } catch (const std::exception& error) {
                        entry.stored = false;
                        storage_failed = true;
                        std::osyncstream(std::cerr) << "[forge] task " << index + 1 << " result storage failed: " << error.what() << '\n';
                    }
                }
            });
        }
    }
    std::string manifest;
    std::size_t succeeded = 0;
    std::cout << "INDEX\tINPUT\tJOB\tSTATE\tEXIT\n";
    for (std::size_t index = 0; index < entries.size(); ++index) {
        auto& entry = entries[index];
        if (!entry.submitted) {
            entry.result.infrastructure_error = true;
            entry.result.error = "not submitted because batch result storage failed";
        }
        if (state(entry) == "SUCCEEDED") ++succeeded;
        manifest += record(index, entry) + '\n';
        std::cout << index + 1 << '\t' << entry.input << '\t' << entry.result.job_id << '\t' << state(entry) << '\t'
                  << (entry.result.infrastructure_error ? "-" : std::to_string(entry.result.exit_code)) << '\n';
    }
    save(directory.get(), "results.jsonl", manifest);
    if (storage_failed) throw std::runtime_error("batch result storage failed; output is incomplete; accepted jobs were not retried");
    std::cerr << "[forge] map: " << succeeded << '/' << entries.size() << " succeeded; results: " << output << '\n';
    return succeeded == entries.size() ? 0 : 1;
}
}
