#!/bin/sh
set -eu

# Direct device installs use a development profile, including optimized Release builds.
# Distribution archives keep the project's production settings.
if [ "$#" -ne 1 ]; then
    echo 'Usage: build-development-device.sh <derived-data-directory>' >&2
    exit 2
fi
IOS_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
xcodebuild -project "$IOS_ROOT/AuroraRelay.xcodeproj" -scheme AuroraRelay \
    -configuration Release -destination 'generic/platform=iOS' -derivedDataPath "$1" \
    APNS_ENVIRONMENT=development RELAY_APNS_ENVIRONMENT=sandbox build

APP_PATH="$1/Build/Products/Release-iphoneos/Relay.app"
ENTITLEMENTS_FILE=$(mktemp)
trap 'rm -f "$ENTITLEMENTS_FILE"' EXIT HUP INT TERM
codesign --verify --deep --strict "$APP_PATH"
codesign -d --entitlements :- "$APP_PATH" > "$ENTITLEMENTS_FILE"
SIGNED_ENV=$(/usr/libexec/PlistBuddy -c 'Print :aps-environment' "$ENTITLEMENTS_FILE")
REGISTERED_ENV=$(/usr/libexec/PlistBuddy -c 'Print :RelayAPNSEnvironment' "$APP_PATH/Info.plist")
if [ "$SIGNED_ENV" != development ] || [ "$REGISTERED_ENV" != sandbox ]; then
    echo 'APNs environment mismatch: device installation must not proceed.' >&2
    exit 1
fi
printf 'Verified development signature and sandbox registration: %s\n' "$APP_PATH"
