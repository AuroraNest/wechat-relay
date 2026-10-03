#!/bin/bash
set -euo pipefail
project=$(cd "$(dirname "$0")" && pwd)
sdk=${ANDROID_SDK_ROOT:-"$HOME/Library/Android/sdk"}
java_home=${JAVA_HOME:-$(/usr/libexec/java_home -v 17)}
tools="$sdk/build-tools/34.0.0"
android="$sdk/platforms/android-34/android.jar"
output=${1:-"$project/build"}
mkdir -p "$output/classes" "$output/dex"
"$java_home/bin/javac" -source 17 -target 17 -classpath "$android" -d "$output/classes" "$project/src/com/aurora/tabletrelay/ReplyInstrumentation.java"
"$java_home/bin/jar" --create --file "$output/classes.jar" -C "$output/classes" .
JAVA_HOME="$java_home" "$tools/d8" --lib "$android" --min-api 34 --output "$output/dex" "$output/classes.jar"
"$tools/aapt2" link -I "$android" --manifest "$project/AndroidManifest.xml" --min-sdk-version 34 --target-sdk-version 34 --version-code 3 --version-name 0.3 -o "$output/unsigned.apk"
zip -q -j "$output/unsigned.apk" "$output/dex/classes.dex"
"$tools/zipalign" -f 4 "$output/unsigned.apk" "$output/aligned.apk"
if [[ ! -f "$output/debug.keystore" ]]; then
  "$java_home/bin/keytool" -genkeypair -keystore "$output/debug.keystore" -storepass android -keypass android -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 -dname 'CN=Android Debug'
fi
JAVA_HOME="$java_home" "$tools/apksigner" sign --ks "$output/debug.keystore" --ks-pass pass:android --key-pass pass:android --out "$output/aurora-tablet-ui.apk" "$output/aligned.apk"
printf '%s\n' "$output/aurora-tablet-ui.apk"
