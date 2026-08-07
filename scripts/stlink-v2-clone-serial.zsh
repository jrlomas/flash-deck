#!/usr/bin/env zsh

set -eu
setopt pipe_fail extended_glob
readonly SCRIPT_DIR=${0:A:h}
readonly REPO_ROOT=${SCRIPT_DIR:h}

readonly DEFAULT_CUBE_ROOT="$HOME/STMicroelectronics/STM32Cube/STM32CubeProgrammer"
readonly DEFAULT_DECRYPTOR="$REPO_ROOT/tools/st-decrypt/dist/st_decrypt.jar"
readonly AES_KEY='best performance'
readonly FIRMWARE_RESOURCE='com/st/stlinkupgrade/core/f2_3.bin'

die() {
  print -u2 -r -- "error: $*"
  exit 1
}

usage() {
  cat <<'EOF'
Prepare or install a unique serial in a standalone ST-LINK/V2 clone.

Usage:
  stlink-v2-clone-serial.zsh prepare SERIAL [OUTPUT_DIR]
  stlink-v2-clone-serial.zsh inspect TOPOLOGY
  stlink-v2-clone-serial.zsh load TOPOLOGY TEMPORARY_FIRMWARE
  stlink-v2-clone-serial.zsh enter TOPOLOGY PATCHED_UPDATER
  stlink-v2-clone-serial.zsh check TOPOLOGY PATCHED_UPDATER [CURRENT_SERIAL]
  stlink-v2-clone-serial.zsh program TOPOLOGY PATCHED_UPDATER

Examples:
  scripts/stlink-v2-clone-serial.zsh prepare F1A5DEC00002
  scripts/stlink-v2-clone-serial.zsh inspect 1-8.3
  scripts/stlink-v2-clone-serial.zsh load 1-8.3 build/stlink-bootloader/F1A5DEC00002/bootdump.bin
  scripts/stlink-v2-clone-serial.zsh enter 1-8.3 build/stlink-serial/F1A5DEC00002/STLinkUpgrade.jar
  scripts/stlink-v2-clone-serial.zsh check 1-8.3 build/stlink-serial/F1A5DEC00002/STLinkUpgrade.jar
  scripts/stlink-v2-clone-serial.zsh program 1-8.3 build/stlink-serial/F1A5DEC00002/STLinkUpgrade.jar

Environment overrides:
  STM32CUBE_PROGRAMMER_ROOT   STM32CubeProgrammer installation root
  STLINK_DECRYPTOR_JAR        Path to lujji/st-decrypt st_decrypt.jar
  STLINK_TOOL                 Path to jeanthom/stlink-tool

Prepare the decryptor dependency with:
  git clone https://github.com/lujji/st-decrypt.git tools/st-decrypt

Safety:
  Every hardware command runs in a bwrap namespace exposing only the exact
  USB topology supplied. `program` requires the probe to already report DFU v1.
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

validate_serial() {
  local serial=$1
  [[ ${#serial} == 12 ]] || die "serial must contain exactly 12 hexadecimal characters"
  [[ $serial == [0-9A-F]## ]] || die "serial must use uppercase hexadecimal characters only"
}

cube_paths() {
  CUBE_ROOT=${STM32CUBE_PROGRAMMER_ROOT:-$DEFAULT_CUBE_ROOT}
  UPGRADE_DIR="$CUBE_ROOT/Drivers/FirmwareUpgrade"
  STOCK_UPDATER="$UPGRADE_DIR/STLinkUpgrade.jar"
  JAVA_BIN="$CUBE_ROOT/bin/jre/bin/java"
  DECRYPTOR=${STLINK_DECRYPTOR_JAR:-$DEFAULT_DECRYPTOR}
  DECRYPTOR_LIB="${DECRYPTOR:h:h}/lib/commons-cli-1.3.1/commons-cli-1.3.1.jar"

  [[ -x $JAVA_BIN ]] || die "CubeProgrammer Java runtime not found: $JAVA_BIN"
  [[ -r $STOCK_UPDATER ]] || die "ST-Link updater not found: $STOCK_UPDATER"
}

run_decryptor() {
  if [[ -r $DECRYPTOR_LIB ]]; then
    "$JAVA_BIN" -cp "$DECRYPTOR:$DECRYPTOR_LIB" st_decrypt.ST_decrypt "$@"
  else
    "$JAVA_BIN" -jar "$DECRYPTOR" "$@"
  fi
}

prepare_updater() {
  local serial=$1
  local requested_output=${2:-"$REPO_ROOT/build/stlink-serial/$serial"}
  local output=${requested_output:A}
  local temp

  validate_serial "$serial"
  cube_paths
  [[ -r $DECRYPTOR ]] || die "st-decrypt jar not found: $DECRYPTOR (clone lujji/st-decrypt under tools/ or set STLINK_DECRYPTOR_JAR)"

  require_command unzip
  require_command zip
  require_command perl
  require_command dd
  require_command sha256sum

  mkdir -p "$output"
  temp=$(mktemp -d)
  trap "rm -rf -- ${(q)temp}" EXIT INT TERM

  unzip -p "$STOCK_UPDATER" "$FIRMWARE_RESOURCE" > "$temp/original.encrypted.bin"
  local image_size=$(wc -c < "$temp/original.encrypted.bin")
  (( image_size > 0 && image_size % 16 == 0 )) || die "unexpected encrypted firmware size: $image_size"

  run_decryptor --key "$AES_KEY" \
    -i "$temp/original.encrypted.bin" -o "$temp/decrypted.padded.bin"
  dd if="$temp/decrypted.padded.bin" of="$temp/decrypted.bin" \
    bs="$image_size" count=1 status=none

  cp "$temp/decrypted.bin" "$temp/patched.decrypted.bin"
  # Match the protected loader descriptor width so both USB modes can expose
  # one identity. Keep the unused tail in place to avoid shifting firmware.
  SERIAL_TO_PATCH=$serial perl -0777 -pi -e '
    BEGIN {
      $s = $ENV{"SERIAL_TO_PATCH"};
      $replacement = "\x1a\x03" . join("", map { $_ . "\x00" } split(//, $s));
    }
    @matches = ($_ =~ /\x32\x03(?:[0-9A-F]\x00){23}[0-9A-F]/g);
    die "expected exactly one legacy USB serial descriptor\n" unless @matches == 1;
    $offset = index($_, $matches[0]);
    substr($_, $offset, length($replacement), $replacement);
  ' "$temp/patched.decrypted.bin"

  # At startup V2J48 replaces the serial descriptor above with one generated
  # from the STM32F1 silicon UID. Many clones expose the same synthetic UID,
  # producing the colliding USB iSerial "000000000001". Keep the patched
  # descriptor by returning immediately from that formatter. Every anchor is
  # checked exactly so a future ST firmware layout change fails closed.
  perl -0777 -pi -e '
    BEGIN {
      $formatter = pack("H*", "f0b4434900bb08684a688968002856d0");
      $return = pack("H*", "704700bf"); # bx lr; nop
    }
    $offset = index($_, $formatter);
    die "USB serial formatter anchor was not unique\n"
      if $offset < 0 || index($_, $formatter, $offset + 1) >= 0;
    substr($_, $offset, length($return), $return);
  ' "$temp/patched.decrypted.bin"

  run_decryptor --key "$AES_KEY" \
    -i "$temp/patched.decrypted.bin" -o "$temp/patched.encrypted.padded.bin" --encrypt
  dd if="$temp/patched.encrypted.padded.bin" of="$temp/f2_3.bin" \
    bs="$image_size" count=1 status=none

  cp "$STOCK_UPDATER" "$output/STLinkUpgrade.jar"
  zip -q -d "$output/STLinkUpgrade.jar" META-INF/ST_PRIVA.SF META-INF/ST_PRIVA.RSA
  mkdir -p "$temp/staging/${FIRMWARE_RESOURCE:h}"
  cp "$temp/f2_3.bin" "$temp/staging/$FIRMWARE_RESOURCE"
  (cd "$temp/staging" && zip -q -u "$output/STLinkUpgrade.jar" "$FIRMWARE_RESOURCE")
  ln -sfn "$UPGRADE_DIR/native" "$output/native"

  cp "$temp/decrypted.bin" "$output/original.decrypted.bin"
  cp "$temp/patched.decrypted.bin" "$output/patched.decrypted.bin"
  cp "$temp/f2_3.bin" "$output/f2_3.encrypted.bin"
  print -r -- "$serial" > "$output/SERIAL"
  sha256sum "$output/STLinkUpgrade.jar" "$output/f2_3.encrypted.bin" > "$output/SHA256SUMS"

  print -r -- "Prepared serial: $serial"
  print -r -- "Updater: $output/STLinkUpgrade.jar"
  print -r -- "Manifest: $output/SHA256SUMS"
}

resolve_probe() {
  local topology=$1
  [[ $topology == [0-9]##-[0-9]##(|.[0-9.]##) ]] || die "invalid USB topology: $topology"

  SYSFS_LINK="/sys/bus/usb/devices/$topology"
  [[ -d $SYSFS_LINK ]] || die "USB topology is not present: $topology"
  SYSFS_REAL=${SYSFS_LINK:A}
  [[ $SYSFS_REAL == /sys/devices/* ]] || die "unexpected sysfs target: $SYSFS_REAL"

  local vid=$(<"$SYSFS_LINK/idVendor")
  local pid=$(<"$SYSFS_LINK/idProduct")
  [[ "$vid:$pid" == 0483:3748 ]] || die "$topology is $vid:$pid, not a standalone ST-LINK/V2 (0483:3748)"

  BUSNUM=$(<"$SYSFS_LINK/busnum")
  DEVNUM=$(<"$SYSFS_LINK/devnum")
  printf -v BUS_PADDED '%03d' "$BUSNUM"
  printf -v DEV_PADDED '%03d' "$DEVNUM"
  USB_NODE="/dev/bus/usb/$BUS_PADDED/$DEV_PADDED"
  [[ -e $USB_NODE ]] || die "USB device node not found: $USB_NODE"
}

inspect_probe() {
  local topology=$1
  resolve_probe "$topology"
  local serial=''
  [[ -r "$SYSFS_LINK/serial" ]] && serial=$(<"$SYSFS_LINK/serial")
  print -r -- "topology=$topology"
  print -r -- "sysfs=$SYSFS_REAL"
  print -r -- "usb_node=$USB_NODE"
  print -r -- "usb_serial=$serial"
}

load_probe() {
  local topology=$1
  local firmware=${2:A}
  local tool=${STLINK_TOOL:-stlink-tool}

  resolve_probe "$topology"
  [[ -r $firmware ]] || die "firmware image not found: $firmware"
  tool=$(command -v "$tool") || die "stlink-tool not found (install it or set STLINK_TOOL)"

  print -r -- "About to load temporary firmware into the application slot on the ST-LINK/V2 at $topology."
  inspect_probe "$topology"
  print -n -r -- "Type the topology '$topology' to continue: "
  local confirmation
  read -r confirmation
  [[ $confirmation == $topology ]] || die "confirmation did not match; nothing was written"

  pkexec /usr/bin/bwrap \
    --ro-bind / / \
    --tmpfs /sys/bus/usb/devices \
    --dir "/sys/bus/usb/devices/$topology" \
    --ro-bind "$SYSFS_REAL" "/sys/bus/usb/devices/$topology" \
    --dev /dev \
    --dir /dev/bus --dir /dev/bus/usb --dir "/dev/bus/usb/$BUS_PADDED" \
    --dev-bind "$USB_NODE" "$USB_NODE" \
    --proc /proc \
    "$tool" "$firmware"
}

isolated_updater() {
  local topology=$1
  local updater=$2
  shift 2

  cube_paths
  resolve_probe "$topology"
  updater=${updater:A}
  [[ -r $updater ]] || die "patched updater not found: $updater"
  [[ -d "${updater:h}/native" ]] || die "native library link not found beside updater: ${updater:h}/native"

  pkexec /usr/bin/bwrap \
    --ro-bind / / \
    --tmpfs /sys/bus/usb/devices \
    --dir "/sys/bus/usb/devices/$topology" \
    --ro-bind "$SYSFS_REAL" "/sys/bus/usb/devices/$topology" \
    --dev /dev \
    --dir /dev/bus --dir /dev/bus/usb --dir "/dev/bus/usb/$BUS_PADDED" \
    --dev-bind "$USB_NODE" "$USB_NODE" \
    --proc /proc --tmpfs /tmp \
    --chdir "${updater:h}" \
    "$JAVA_BIN" -jar "$updater" "$@"
}

check_probe() {
  local topology=$1
  local updater=$2
  local current_serial=${3:-}

  if [[ -n $current_serial ]]; then
    isolated_updater "$topology" "$updater" -sn "$current_serial" -checkVer
  else
    isolated_updater "$topology" "$updater" -checkDfuVer
  fi
}

enter_probe() {
  local topology=$1
  local updater=$2
  isolated_updater "$topology" "$updater" -checkVer
}

program_probe() {
  local topology=$1
  local updater=$2

  print -r -- "About to force-program only the ST-LINK/V2 at USB topology $topology."
  inspect_probe "$topology"
  print -n -r -- "Type the topology '$topology' to continue: "
  local confirmation
  read -r confirmation
  [[ $confirmation == $topology ]] || die "confirmation did not match; nothing was written"

  local check_output
  check_output=$(isolated_updater "$topology" "$updater" -checkDfuVer 2>&1) || {
    print -u2 -r -- "$check_output"
    die "DFU check failed; nothing was written"
  }
  print -r -- "$check_output"
  [[ $check_output == *'DFU v1'* ]] || die "probe is not in DFU v1; nothing was written"

  isolated_updater "$topology" "$updater" -force_prog
}

(( $# >= 1 )) || { usage; exit 2; }
command_name=$1
shift

case $command_name in
  prepare)
    (( $# >= 1 && $# <= 2 )) || die "prepare requires SERIAL and optional OUTPUT_DIR"
    prepare_updater "$@"
    ;;
  inspect)
    (( $# == 1 )) || die "inspect requires TOPOLOGY"
    inspect_probe "$1"
    ;;
  load)
    (( $# == 2 )) || die "load requires TOPOLOGY and TEMPORARY_FIRMWARE"
    load_probe "$@"
    ;;
  enter)
    (( $# == 2 )) || die "enter requires TOPOLOGY and PATCHED_UPDATER"
    enter_probe "$@"
    ;;
  check)
    (( $# >= 2 && $# <= 3 )) || die "check requires TOPOLOGY, PATCHED_UPDATER, and optional CURRENT_SERIAL"
    check_probe "$@"
    ;;
  program)
    (( $# == 2 )) || die "program requires TOPOLOGY and PATCHED_UPDATER"
    program_probe "$1" "$2"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    die "unknown command: $command_name"
    ;;
esac
