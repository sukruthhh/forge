#pragma once
#include <cstdint>
#include <string>
#include <vector>
#include "forge/workload.hpp"
namespace forge {
struct TaskRequest {
    std::uint32_t cpu = 1;
    std::uint64_t memory = 0;
    std::uint32_t timeout = 30;
    std::string architecture = "native";
    std::vector<std::string> arguments;
    std::vector<InputFile> files;
    std::uint32_t retries = 0;
    bool enforce_limits = false;
};
struct TaskResult {
    std::string job_id;
    int exit_code = 1;
    std::string output;
    std::string error;
    bool timed_out = false;
    bool truncated = false;
    bool infrastructure_error = false;
    bool cancelled = false;
    std::string cancellation_cleanup{};
};
void validate_task(const TaskRequest&);
TaskResult submit_task(std::uint16_t port, const std::string& host, const TaskRequest&);
std::size_t staging_worker_count(std::uint16_t port, const std::string& host, const TaskRequest&);
int coordinator(std::uint16_t port, const std::string& bind);
int join(std::uint16_t port, const std::string& id, const std::string& host, const std::string& cgroup_root = {});
int cluster(std::uint16_t port, const std::string& host, bool details = false);
int ps(std::uint16_t port, const std::string& host);
int logs(std::uint16_t port, const std::string& id, const std::string& host);
int timings(std::uint16_t port, const std::string& id, const std::string& host);
int attempts(std::uint16_t port, const std::string& id, const std::string& host);
int cancel(std::uint16_t port, const std::string& id, const std::string& host);
int run(std::uint16_t port, const std::string& host, const TaskRequest&);
int map(std::uint16_t port, const std::string& host, const TaskRequest&,
        const std::vector<std::string>& inputs, std::size_t concurrency, const std::string& output);
}
