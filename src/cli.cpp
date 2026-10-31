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
                     "  forge cluster [--port 9000]\n\n"
                     "Loopback only; no job execution or authentication yet.\n";
        return 0;
    }
    const std::string_view command(argv[1]);
    std::uint16_t port = 9000;
    std::string id;
    bool port_seen = false;
    for (int i = 2; i < argc; i += 2) {
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
        } else throw std::runtime_error("unknown or repeated option");
    }
    if (command == "coordinator") return forge::coordinator(port);
    if (command == "cluster") return forge::cluster(port);
    if (command == "join" && !id.empty()) return forge::join(port, id);
    throw std::runtime_error("unsupported command; run forge --help");
  } catch (const std::exception& error) {
    std::cerr << "forge: " << error.what() << '\n';
    return 1;
  }
}
