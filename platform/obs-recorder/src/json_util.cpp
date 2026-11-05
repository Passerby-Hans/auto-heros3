#include "json_util.hpp"

#include <cctype>
#include <cstdlib>
#include <cstring>

namespace a3 {

std::string json_escape(const std::string& s)
{
    std::string out;
    out.reserve(s.size() + 8);
    for (unsigned char c : s) {
        switch (c) {
        case '"':
            out += "\\\"";
            break;
        case '\\':
            out += "\\\\";
            break;
        case '\b':
            out += "\\b";
            break;
        case '\f':
            out += "\\f";
            break;
        case '\n':
            out += "\\n";
            break;
        case '\r':
            out += "\\r";
            break;
        case '\t':
            out += "\\t";
            break;
        default:
            if (c < 0x20) {
                char buf[8];
                std::snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned>(c));
                out += buf;
            } else {
                out += static_cast<char>(c);
            }
            break;
        }
    }
    return out;
}

namespace {

const char* find_value_start(const std::string& line, const char* key)
{
    const std::string needle = std::string("\"") + key + "\"";
    std::size_t pos = line.find(needle);
    if (pos == std::string::npos)
        return nullptr;
    pos += needle.size();
    while (pos < line.size() && std::isspace(static_cast<unsigned char>(line[pos])))
        ++pos;
    if (pos >= line.size() || line[pos] != ':')
        return nullptr;
    ++pos;
    while (pos < line.size() && std::isspace(static_cast<unsigned char>(line[pos])))
        ++pos;
    if (pos >= line.size())
        return nullptr;
    return line.c_str() + pos;
}

} // namespace

bool json_extract_string(const std::string& line, const char* key, std::string* out,
                         bool* found)
{
    *found = false;
    const char* start = find_value_start(line, key);
    if (!start)
        return line.find(std::string("\"") + key + "\"") == std::string::npos;
    if (*start != '"')
        return false;
    ++start;
    std::string value;
    while (*start) {
        char c = *start++;
        if (c == '\\') {
            char escaped = *start++;
            if (!escaped)
                return false;
            switch (escaped) {
            case '"':
                value += '"';
                break;
            case '\\':
                value += '\\';
                break;
            case '/':
                value += '/';
                break;
            case 'n':
                value += '\n';
                break;
            case 'r':
                value += '\r';
                break;
            case 't':
                value += '\t';
                break;
            case 'b':
                value += '\b';
                break;
            case 'f':
                value += '\f';
                break;
            case 'u':
                // Keep the escape visible rather than inventing a decoded value.
                if (std::strlen(start) < 4)
                    return false;
                value += "\\u";
                value.append(start, 4);
                start += 4;
                break;
            default:
                return false;
            }
            continue;
        }
        if (c == '"') {
            *out = value;
            *found = true;
            return true;
        }
        value += c;
    }
    return false;
}

bool json_extract_int(const std::string& line, const char* key, long long* out,
                      bool* found)
{
    *found = false;
    const char* start = find_value_start(line, key);
    if (!start)
        return line.find(std::string("\"") + key + "\"") == std::string::npos;
    if (*start == '"')
        return false;
    char* end = nullptr;
    long long value = std::strtoll(start, &end, 10);
    if (end == start)
        return false;
    *out = value;
    *found = true;
    return true;
}

bool json_extract_bool(const std::string& line, const char* key, bool* out, bool* found)
{
    *found = false;
    const char* start = find_value_start(line, key);
    if (!start)
        return line.find(std::string("\"") + key + "\"") == std::string::npos;
    if (std::strncmp(start, "true", 4) == 0) {
        *out = true;
        *found = true;
        return true;
    }
    if (std::strncmp(start, "false", 5) == 0) {
        *out = false;
        *found = true;
        return true;
    }
    return false;
}

} // namespace a3
