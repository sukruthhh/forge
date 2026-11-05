#pragma once
#include "forge/network.hpp"
#include "forge/scheduler.hpp"
#include <filesystem>
#include <stdexcept>

namespace forge {
Resources constrain_capacity(Resources, const std::filesystem::path& leaf,
                             const std::filesystem::path& boundary);
Resources resource_capacity(const std::filesystem::path& additional = {});

class ResourceCleanupError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class CgroupRoot {
    net::Socket directory_;
    friend class CgroupJob;
public:
    explicit CgroupRoot(const std::string& path);
};

class CgroupJob {
    const CgroupRoot& root_;
    net::Socket directory_;
    std::string name_;
    bool cleaned_ = false;
public:
    CgroupJob(const CgroupRoot&, Resources);
    ~CgroupJob();
    CgroupJob(const CgroupJob&) = delete;
    CgroupJob& operator=(const CgroupJob&) = delete;
    int descriptor() const { return directory_.get(); }
    void kill() const;
    void cleanup();
    bool oom_killed() const;
};

int cgroup_exec(int descriptor, char* const arguments[]);
}
