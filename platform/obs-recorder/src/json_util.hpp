// Minimal JSON writer for the obs-recorder S0/S1a host scaffold.
// No third-party dependency; enough for capabilities + JSON-lines IPC.
#pragma once

#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

namespace a3 {

// Escape a UTF-8 string for inclusion inside JSON double quotes.
std::string json_escape(const std::string& s);

class JsonWriter {
public:
    explicit JsonWriter(bool pretty = true, int indent = 2)
        : pretty_(pretty), indent_(indent)
    {
    }

    void begin_object()
    {
        pre_value();
        stack_.push_back(Frame{true, true});
        out_ += '{';
    }

    void end_object() { end_container(true); }

    void begin_array()
    {
        pre_value();
        stack_.push_back(Frame{false, true});
        out_ += '[';
    }

    void end_array() { end_container(false); }

    void key(const std::string& k)
    {
        pre_value();
        out_ += '"';
        out_ += json_escape(k);
        out_ += '"';
        out_ += pretty_ ? ": " : ":";
        pending_value_ = true;
    }

    void value_string(const std::string& v)
    {
        pre_value();
        out_ += '"';
        out_ += json_escape(v);
        out_ += '"';
    }

    void value_string(const char* v) { value_string(std::string(v ? v : "")); }
    void value_bool(bool v) { pre_value(); out_ += v ? "true" : "false"; }
    void value_null() { pre_value(); out_ += "null"; }
    void value_int(long long v) { pre_value(); out_ += std::to_string(v); }
    void value_uint(unsigned long long v) { pre_value(); out_ += std::to_string(v); }

    // Process addresses are opaque evidence; emit them as quoted hex strings
    // (64-bit values are not safely representable as JSON numbers in JS).
    void value_uintptr(std::uintptr_t v)
    {
        pre_value();
        char buf[32];
        std::snprintf(buf, sizeof(buf), "0x%llx",
                      static_cast<unsigned long long>(v));
        out_ += '"';
        out_ += buf;
        out_ += '"';
    }

    void value_double(double v, int decimals = 3)
    {
        pre_value();
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.*f", decimals, v);
        out_ += buf;
    }

    std::string str() const { return out_; }

private:
    struct Frame {
        bool object;
        bool first;
    };

    void append_indent(std::size_t depth)
    {
        out_.append(depth * static_cast<std::size_t>(indent_), ' ');
    }

    void pre_value()
    {
        if (pending_value_) {
            pending_value_ = false;
            return;
        }
        if (stack_.empty())
            return;
        Frame& f = stack_.back();
        if (!f.first)
            out_ += ',';
        f.first = false;
        if (pretty_) {
            out_ += '\n';
            append_indent(stack_.size());
        }
    }

    void end_container(bool object)
    {
        if (stack_.empty())
            return;
        Frame f = stack_.back();
        stack_.pop_back();
        if (pretty_ && !f.first) {
            out_ += '\n';
            append_indent(stack_.size());
        }
        out_ += object ? '}' : ']';
    }

    bool pretty_;
    int indent_;
    std::string out_;
    std::vector<Frame> stack_;
    bool pending_value_ = false;
};

} // namespace a3
