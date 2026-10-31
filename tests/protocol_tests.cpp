#include "forge/network.hpp"
#include <cstdlib>
#include <iostream>
#include <stdexcept>

void check(bool condition, const char* message) {
    if (!condition) { std::cerr << message << '\n'; std::exit(1); }
}
template<class F> void rejects(F operation) {
    try { operation(); } catch (const std::runtime_error&) { return; }
    check(false, "expected protocol rejection");
}
int main() {
    using namespace forge::net;
    Message message{Type::register_worker, {"worker", "", std::string("a\0b", 3)}};
    auto bytes = encode(message);
    auto decoded = decode({bytes.begin() + 4, bytes.end()});
    check(decoded.type == message.type && decoded.fields == message.fields, "round trip failed");
    auto boundary = encode({Type::error, {std::string(max_frame - 10, 'x')}});
    check(boundary.size() == max_frame + 4, "boundary frame size");
    check(decode({boundary.begin() + 4, boundary.end()}).fields[0].size() == max_frame - 10,
          "boundary frame decode");
    rejects([] { encode({Type::error, {std::string(max_frame - 9, 'x')}}); });
    rejects([] { encode({Type::ok, std::vector<std::string>(max_fields + 1)}); });
    rejects([] { decode({1, 4}); });
    rejects([] { decode({2, 4, 0, 0, 0, 0}); });
    rejects([] { decode({1, 99, 0, 0, 0, 0}); });
    rejects([] { decode({1, 4, 0, 0, 0, 0, 0}); });
    rejects([] { decode({1, 4, 0, 0, 0, 1, 255, 255, 255, 255}); });
    std::cout << "Protocol checks passed\n";
}
