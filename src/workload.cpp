#include "forge/workload.hpp"
#include "forge/network.hpp"
#include <algorithm>
#include <cerrno>
#include <charconv>
#include <cstdlib>
#include <filesystem>
#include <fcntl.h>
#include <iostream>
#include <set>
#include <stdexcept>
#include <sys/stat.h>
#include <unistd.h>

namespace forge {
void validate_inputs(const std::vector<InputFile>& files) {
    if (files.size() > max_input_files) throw std::runtime_error("at most 16 staged files per task");
    std::set<std::string> names;
    std::size_t bytes = 0;
    for (const auto& file : files) {
        if (file.name.empty() || file.name.size() > 64 || file.name == "." || file.name == ".." ||
            !std::all_of(file.name.begin(), file.name.end(), [](unsigned char c) {
                return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                       (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-';
            })) throw std::runtime_error("staged file names must be flat 1-64 character ASCII labels, excluding . and ..");
        if (!names.insert(file.name).second) throw std::runtime_error("duplicate staged file name");
        if (file.data.size() > max_input_bytes - bytes)
            throw std::runtime_error("staged file data exceeds 32768 bytes per task");
        bytes += file.data.size();
    }
}
std::vector<InputFile> read_inputs(const std::vector<std::string>& paths) {
    if (paths.size() > max_input_files) throw std::runtime_error("at most 16 staged files per task");
    std::vector<InputFile> files;
    for (const auto& path : paths) {
        net::Socket source(::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK));
        struct stat metadata{};
        if (source.get() < 0 || ::fstat(source.get(), &metadata) != 0 || !S_ISREG(metadata.st_mode))
            throw std::runtime_error("input must be a readable regular file, not a symlink: " + path);
        InputFile file{std::filesystem::path(path).filename().string(), {}, (metadata.st_mode & 0111) != 0};
        char buffer[4096];
        for (;;) {
            const auto count = ::read(source.get(), buffer, sizeof(buffer));
            if (count < 0) {
                if (errno == EINTR) continue;
                throw std::runtime_error("cannot read input: " + path);
            }
            if (count == 0) break;
            if (static_cast<std::size_t>(count) > max_input_bytes - file.data.size())
                throw std::runtime_error("staged file data exceeds 32768 bytes per task");
            file.data.append(buffer, static_cast<std::size_t>(count));
        }
        files.push_back(std::move(file));
        validate_inputs(files);
    }
    return files;
}
void append_inputs(std::vector<std::string>& fields, const std::vector<InputFile>& files) {
    validate_inputs(files);
    fields.push_back(std::to_string(files.size()));
    for (const auto& file : files)
        fields.insert(fields.end(), {file.name, file.executable ? "1" : "0", file.data});
}
std::vector<InputFile> parse_inputs(const std::vector<std::string>& fields, std::size_t& offset) {
    if (offset >= fields.size()) throw std::runtime_error("missing staged file count");
    const auto& text = fields[offset++];
    std::size_t count = 0;
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), count);
    if (error != std::errc{} || end != text.data() + text.size() || count == 0 ||
        count > max_input_files || count > (fields.size() - offset) / 3)
        throw std::runtime_error("invalid staged file count");
    std::vector<InputFile> files;
    for (std::size_t i = 0; i < count; ++i) {
        if (fields[offset + 1] != "0" && fields[offset + 1] != "1")
            throw std::runtime_error("invalid staged executable flag");
        files.push_back({fields[offset], fields[offset + 2], fields[offset + 1] == "1"});
        offset += 3;
    }
    validate_inputs(files);
    return files;
}
Workspace::Workspace(const std::vector<InputFile>& files) {
    validate_inputs(files);
    char pattern[] = "/tmp/forge-job-XXXXXX";
    const char* directory = ::mkdtemp(pattern);
    if (!directory) throw std::runtime_error("cannot create task workspace");
    path_ = directory;
    try {
        net::Socket root(::open(path_.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
        if (root.get() < 0) throw std::runtime_error("cannot open task workspace");
        for (const auto& file : files) {
            net::Socket destination(::openat(root.get(), file.name.c_str(),
                O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600));
            if (destination.get() < 0) throw std::runtime_error("cannot create staged file");
            std::size_t offset = 0;
            while (offset < file.data.size()) {
                const auto count = ::write(destination.get(), file.data.data() + offset, file.data.size() - offset);
                if (count < 0 && errno == EINTR) continue;
                if (count <= 0) throw std::runtime_error("cannot write staged file");
                offset += static_cast<std::size_t>(count);
            }
            if (::fchmod(destination.get(), file.executable ? 0700 : 0600) != 0)
                throw std::runtime_error("cannot set staged file permissions");
        }
    } catch (...) {
        std::error_code error;
        std::filesystem::remove_all(path_, error);
        throw;
    }
}
Workspace::~Workspace() {
    std::error_code error;
    std::filesystem::remove_all(path_, error);
    if (error) std::cerr << "[forge] workspace cleanup failed: " << path_ << '\n';
}
}
