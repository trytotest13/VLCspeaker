# Generates wix/main-rust.wxs: one MSI component per file under piper/,
# plus the app exe, shortcuts and uninstall entry.
# Run from the repo root:  python wix/gen_wxs.py
# Then:  wix build -arch x64 -o dist/VLCSpeaker-Setup-<VERSION>.msi wix/main-rust.wxs
import os
import uuid

VERSION = "2.0.2"
BS = chr(92)  # backslash, kept out of string literals to avoid escape traps


def guid_for(key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "vlcspeaker-msi:" + key.replace(BS, "/"))).upper()


comps = []
body = []
out = body.append


def emit_file(rel, dir_id):
    rel = rel.replace("/", BS)
    cid = "c_" + uuid.uuid5(uuid.NAMESPACE_URL, "c:" + rel.replace(BS, "/")).hex[:16]
    fid = "f_" + uuid.uuid5(uuid.NAMESPACE_URL, "f:" + rel.replace(BS, "/")).hex[:16]
    comps.append(cid)
    out(f'            <Component Id="{cid}" Guid="{guid_for(rel)}" Directory="{dir_id}">')
    out(f'                <File Id="{fid}" Name="{os.path.basename(rel)}" Source="{rel}" />')
    out('            </Component>')


all_files, all_dirs = [], []
for root, dirs, files in os.walk("piper/espeak-ng-data"):
    r = root.replace(os.sep, "/")
    for f in sorted(files):
        all_files.append(r + "/" + f)
    for d in dirs:
        all_dirs.append(r + "/" + d)


def emit_tree(dir_id, base_n):
    for rel in sorted(all_files):
        if os.path.dirname(rel) == base_n:
            emit_file(rel, dir_id)
    # derive child dirs from the walk itself: dirs with no direct files of
    # their own (e.g. voices/!v) would otherwise be skipped
    for child in sorted(all_dirs):
        if os.path.dirname(child) == base_n:
            cid = "d_" + uuid.uuid5(uuid.NAMESPACE_URL, "dir:" + child).hex[:16]
            out(f'            <Directory Id="{cid}" Name="{os.path.basename(child)}">')
            emit_tree(cid, child)
            out('            </Directory>')


out('<?xml version="1.0" encoding="UTF-8"?>')
out('<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">')
out('  <Package Name="VLC Speaker" Manufacturer="VLC Speaker" Version="' + VERSION + '"')
out('           UpgradeCode="8F9A2B3C-4D5E-6F7A-8B9C-0D1E2F3A4B5C"')
out('           Scope="perMachine" Compressed="yes" Language="1033" InstallerVersion="500">')
out('    <MajorUpgrade DowngradeErrorMessage="A newer version of [ProductName] is already installed." AllowSameVersionUpgrades="yes" />')
out('    <MediaTemplate EmbedCab="yes" />')
out('    <Icon Id="AppIco" SourceFile="assets' + BS + 'icon.ico" />')
out('    <Property Id="ARPPRODUCTICON" Value="AppIco" />')
out('    <StandardDirectory Id="ProgramFiles64Folder">')
out('      <Directory Id="INSTALLFOLDER" Name="VLCSpeaker">')
out('        <Component Id="MainExe" Guid="' + guid_for("vlcspeaker.exe") + '">')
out('          <File Id="ExeFile" Name="vlcspeaker.exe" Source="target' + BS + 'release' + BS + 'vlcspeaker.exe" KeyPath="yes" />')
out('        </Component>')
out('        <Directory Id="PIPER" Name="piper">')
for f in sorted(os.listdir("piper")):
    if os.path.isfile(os.path.join("piper", f)):
        emit_file("piper/" + f, "PIPER")
out('          <Directory Id="ESPEAK" Name="espeak-ng-data">')
emit_tree("ESPEAK", "piper/espeak-ng-data")
out('          </Directory>')
out('          <Directory Id="VOICES" Name="voices">')
for f in sorted(os.listdir("piper/voices")):
    if os.path.isfile(os.path.join("piper/voices", f)):
        emit_file("piper/voices/" + f, "VOICES")
out('          </Directory>')
out('        </Directory>')
out('      </Directory>')
out('    </StandardDirectory>')
out('    <StandardDirectory Id="ProgramMenuFolder">')
out('      <Directory Id="AppMenuDir" Name="VLC Speaker">')
out('        <Component Id="StartMenuShortcuts" Guid="' + guid_for("startmenu") + '">')
out('          <Shortcut Id="AppShortcut" Name="VLC Speaker" Description="Watch foreign-language movies by listening"')
out('                    Target="[INSTALLFOLDER]vlcspeaker.exe" WorkingDirectory="INSTALLFOLDER" />')
out('          <RemoveFolder Id="RemoveAppMenuDir" Directory="AppMenuDir" On="uninstall" />')
out('          <RegistryValue Root="HKCU" Key="Software' + BS + 'VLCSpeaker" Name="StartMenuShortcut" Type="integer" Value="1" KeyPath="yes" />')
out('        </Component>')
out('      </Directory>')
out('    </StandardDirectory>')
out('    <StandardDirectory Id="DesktopFolder">')
out('      <Component Id="DesktopShortcut" Guid="' + guid_for("desktop") + '">')
out('        <Shortcut Id="DesktopShortcutLink" Name="VLC Speaker" Target="[INSTALLFOLDER]vlcspeaker.exe" WorkingDirectory="INSTALLFOLDER" />')
out('        <RegistryValue Root="HKCU" Key="Software' + BS + 'VLCSpeaker" Name="DesktopShortcut" Type="integer" Value="1" KeyPath="yes" />')
out('      </Component>')
out('    </StandardDirectory>')
out('    <Feature Id="Main" Level="1">')
out('      <ComponentRef Id="MainExe" />')
for cid in comps:
    out(f'      <ComponentRef Id="{cid}" />')
out('      <ComponentRef Id="StartMenuShortcuts" />')
out('      <ComponentRef Id="DesktopShortcut" />')
out('    </Feature>')
out('  </Package>')
out('</Wix>')

with open("wix/main-rust.wxs", "w", encoding="utf-8") as fh:
    fh.write("\n".join(body) + "\n")

disk = sum(len(f) for _, _, f in os.walk("piper"))
print(f"wix/main-rust.wxs written: {len(comps)} piper components, {disk + 1} files total")
