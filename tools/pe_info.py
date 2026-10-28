# -*- coding: utf-8 -*-
"""Dump PE metadata of Heroes3 executables: version, sections, imports, hashes."""
import sys, hashlib
import pefile

INTERESTING = ("DirectDraw", "CreateFile", "ReadFile", "WriteFile", "VirtualAlloc",
               "GetProcAddr", "LoadLibrary", "SetCursor", "ShowCursor", "ClipCursor",
               "GetKeyState", "GetAsyncKeyState", "MessageBox", "PostMessage", "SendMessage",
               "BitBlt", "CreateDIBSection", "timeGetTime", "GetCurrentDirectory",
               "WideCharToMultiByte", "MultiByteToWideChar")


def dump(path):
    data = open(path, 'rb').read()
    print("=" * 78)
    print(f"file   : {path}")
    print(f"size   : {len(data)}")
    print(f"md5    : {hashlib.md5(data).hexdigest()}")
    print(f"sha256 : {hashlib.sha256(data).hexdigest()}")
    pe = pefile.PE(path)
    ts = pe.FILE_HEADER.TimeDateStamp
    print(f"machine: {hex(pe.FILE_HEADER.Machine)}  timestamp: {ts}  "
          f"subsystem: {pe.OPTIONAL_HEADER.Subsystem}  imagebase: {hex(pe.OPTIONAL_HEADER.ImageBase)}")
    if ts:
        import datetime
        print(f"linkage: {datetime.datetime.utcfromtimestamp(ts)} UTC")
    # version info
    try:
        pe.parse_data_directories()
        for fi in getattr(pe, 'FileInfo', []) or []:
            for entry in fi:
                if entry.Key == b'StringFileInfo':
                    for st in entry.StringTable:
                        for k, v in st.entries.items():
                            print(f"verinfo: {k.decode()} = {v.decode()}")
    except Exception as e:
        print(f"verinfo: none ({e})")
    print("-- sections --")
    for s in pe.sections:
        name = s.Name.rstrip(b'\x00').decode(errors='replace')
        ent = s.get_entropy()
        print(f"  {name:10s} va={s.VirtualAddress:#010x} vsz={s.Misc_VirtualSize:#010x} "
              f"raw={s.SizeOfRawData:#010x} entropy={ent:.2f}")
    print("-- imports (interesting) --")
    try:
        for dll, funcs in pe.DIRECTORY_ENTRY_IMPORT.items() if isinstance(pe.DIRECTORY_ENTRY_IMPORT, dict) else []:
            pass
    except Exception:
        pass
    if hasattr(pe, 'DIRECTORY_ENTRY_IMPORT'):
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            dll = entry.dll.decode()
            names = [imp.name.decode() for imp in entry.imports if imp.name]
            hits = [n for n in names if any(k.lower() in n.lower() for k in INTERESTING)]
            print(f"  {dll}: {len(names)} imports"
                  + (f" | e.g. {', '.join(hits[:12])}" if hits else ""))
    else:
        print("  (no import directory - packed?)")


if __name__ == '__main__':
    for p in sys.argv[1:]:
        try:
            dump(p)
        except Exception as e:
            print(f"{p}: FAILED - {e}")
