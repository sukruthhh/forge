#include "forge/runtime.hpp"
#include <charconv>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>

int main(int argc, char** argv) {
  try {
    if (argc == 2 && std::string_view(argv[1]) == "--version") {
        std::cout << "forge 0.1.0-dev\n";
        return 0;
    }
    if (argc == 1 || (argc == 2 && std::string_view(argv[1]) == "--help")) {
        std::cout << "Forge - private batch compute across your machines\n\n"
                     "Usage:\n  forge coordinator [--port 9000]\n"
                     "  forge join --id NAME [--port 9000]\n"
                     "  forge cluster [--port 9000]\n"
                     "  forge ps [--port 9000]\n"
                     "  forge logs JOB [--port 9000]\n\n"
                     "  forge run [--port 9000] [--cpu 1] [--memory 0] [--timeout 30] -- COMMAND [ARGS...]\n\n"
                     "Loopback only; trusted commands, no sandbox. Set FORGE_TOKEN on all peers to require authentication.\n";
        return 0;
    }
    const std::string_view command(argv[1]);
    std::uint16_t port = 9000;
    std::string id, log_id;
    std::uint32_t cpu = 1, timeout = 30;
    std::uint64_t memory = 0;
    bool cpu_seen = false, memory_seen = false, timeout_seen = false;
    std::vector<std::string> arguments;
    bool port_seen = false;
    int first_option = 2;
    if (command == "logs") {
        if (argc < 3) throw std::runtime_error("usage: forge logs JOB [--port PORT]");
        log_id = argv[2];
        first_option = 3;
    }
    for (int i = first_option; i < argc; i += 2) {
        if (command == "run" && std::string_view(argv[i]) == "--") {
            for (++i; i < argc; ++i) arguments.emplace_back(argv[i]);
            break;
        }
        if (i + 1 >= argc) throw std::runtime_error("option needs a value");
        const std::string_view option(argv[i]);
        const std::string_view value(argv[i + 1]);
        if (option == "--port" && !port_seen) {
            unsigned parsed = 0;
            const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), parsed);
            if (error != std::errc{} || end != value.data() + value.size() || parsed == 0 || parsed > 65535)
                throw std::runtime_error("port must be between 1 and 65535");
            port = static_cast<std::uint16_t>(parsed);
            port_seen = true;
        } else if (option == "--id" && command == "join" && id.empty()) {
            id = value;
        } else if (command == "run" &&
                   (option == "--cpu" || option == "--memory" || option == "--timeout")) {
            std::uint64_t parsed = 0;
            const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), parsed);
            if (error != std::errc{} || end != value.data() + value.size())
                throw std::runtime_error("invalid numeric job option");
            if (option == "--cpu" && !cpu_seen && parsed > 0 && parsed <= 1048576) {
                cpu = static_cast<std::uint32_t>(parsed); cpu_seen = true;
            } else if (option == "--memory" && !memory_seen) {
                memory = parsed; memory_seen = true;
            } else if (option == "--timeout" && !timeout_seen && parsed > 0 && parsed <= 30) {
                timeout = static_cast<std::uint32_t>(parsed); timeout_seen = true;
            } else throw std::runtime_error("repeated or out-of-range job option");
        } else throw std::runtime_error("unknown or repeated option");
    }
    if (command == "coordinator") return forge::coordinator(port);
    if (command == "cluster") return forge::cluster(port);
    if (command == "ps") return forge::ps(port);
    if (command == "logs") return forge::logs(port, log_id);
    if (command == "join" && !id.empty()) return forge::join(port, id);
    if (command == "run" && !arguments.empty()) return forge::run(port, cpu, memory, timeout, arguments);
    throw std::runtime_error("unsupported command; run forge --help");
  } catch (const std::exception& error) {
    std::cerr << "forge: " << error.what() << '\n';
    return 1;
  }
}
