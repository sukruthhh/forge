#pragma once
#include <cstddef>
#include <string>
#include <vector>

namespace forge {
struct InputFile {
    std::string name;
    std::string data;
    bool executable = false;
};
constexpr std::size_t max_input_files = 16;
constexpr std::size_t max_input_bytes = 32768;
void validate_inputs(const std::vector<InputFile>&);
std::vector<InputFile> read_inputs(const std::vector<std::string>& paths);
void append_inputs(std::vector<std::string>& fields, const std::vector<InputFile>&);
std::vector<InputFile> parse_inputs(const std::vector<std::string>& fields, std::size_t& offset);

class Workspace {
public:
    explicit Workspace(const std::vector<InputFile>&);
    ~Workspace();
    Workspace(const Workspace&) = delete;
    Workspace& operator=(const Workspace&) = delete;
    const std::string& path() const { return path_; }
private:
    std::string path_;
};
}
