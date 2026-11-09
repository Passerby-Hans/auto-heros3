#include "../src/media/ipc_owner_policy.hpp"
#include <cstdio>

int main()
{
    using a3::DecideOwnerConnection;
    using Decision = a3::OwnerConnectDecision;
    unsigned checks = 0, failures = 0;
#define CHECK(expr) do { ++checks; if (!(expr)) { ++failures; std::printf("FAIL line=%d\n", __LINE__); } } while (0)
    CHECK(DecideOwnerConnection(false, 0, false, 0, 123) == Decision::OwnerUnavailable);
    CHECK(DecideOwnerConnection(false, 1, true, 123, 123) == Decision::OwnerUnavailable);
    CHECK(DecideOwnerConnection(true, 0, false, 0, 0) == Decision::OwnerUnavailable);
    CHECK(DecideOwnerConnection(true, 0, true, 123, 123) == Decision::Accept);
    CHECK(DecideOwnerConnection(true, 10, true, 456, 123) == Decision::WrongClient);
    CHECK(DecideOwnerConnection(true, 10, true, 0, 123) == Decision::WrongClient);
    CHECK(DecideOwnerConnection(true, 29999, false, 0, 123) == Decision::Wait);
    CHECK(DecideOwnerConnection(true, 30000, false, 0, 123) == Decision::Timeout);
    CHECK(DecideOwnerConnection(true, 30000, true, 123, 123) == Decision::Timeout);
    // Simulate MCP death during first-connect polling, before any media/IPC.
    for (uint64_t elapsed = 0; elapsed <= 1000; elapsed += 25) {
        CHECK(DecideOwnerConnection(elapsed < 500, elapsed, false, 0, 123) ==
              (elapsed < 500 ? Decision::Wait : Decision::OwnerUnavailable));
    }
    // A replacement PID cannot be accepted merely because a peer connected.
    CHECK(DecideOwnerConnection(false, 400, true, 123, 123) == Decision::OwnerUnavailable);
    using a3::DecideShutdownReceipt;
    using Receipt = a3::ShutdownReceiptDecision;
    CHECK(DecideShutdownReceipt(false, true, true, 0) == Receipt::Wait);
    CHECK(DecideShutdownReceipt(false, true, true, 999) == Receipt::Wait);
    CHECK(DecideShutdownReceipt(false, true, true, 1000) == Receipt::Timeout);
    CHECK(DecideShutdownReceipt(false, true, true, 10000) == Receipt::Timeout);
    CHECK(DecideShutdownReceipt(true, true, true, 1) == Receipt::Acknowledged);
    CHECK(DecideShutdownReceipt(false, false, true, 1) == Receipt::OwnerUnavailable);
    CHECK(DecideShutdownReceipt(false, true, false, 1) == Receipt::PipeClosed);
    // Delayed receipt is accepted only after the controller actually read the
    // response; until then the state remains bounded and explicitly pending.
    for (uint64_t elapsed = 0; elapsed <= 500; elapsed += 5) {
        CHECK(DecideShutdownReceipt(elapsed >= 500, true, true, elapsed) ==
              (elapsed < 500 ? Receipt::Wait : Receipt::Acknowledged));
    }
    std::printf("%u/%u checks passed\n", checks - failures, checks);
    return failures ? 1 : 0;
}
