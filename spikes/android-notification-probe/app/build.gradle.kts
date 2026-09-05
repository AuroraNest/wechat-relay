import java.net.URI
import java.util.Properties

plugins {
    id("com.android.application")
    id("com.google.devtools.ksp")
}

val relayEnvironment = providers.gradleProperty("relayEnvironment").orElse("development").get()
require(relayEnvironment in setOf("development", "production")) { "Unknown relayEnvironment" }
val localRelayProperties = Properties()
val localRelayFile = rootProject.file("relay.$relayEnvironment.properties")
if (localRelayFile.isFile) localRelayFile.inputStream().use { localRelayProperties.load(it) }
val relayOrigin = providers.gradleProperty("relayOrigin")
    .orElse(localRelayProperties.getProperty("relayOrigin", "https://relay.example.com")).get()
val relayUri = URI(relayOrigin)
require(relayUri.scheme == "https" && relayUri.host != null &&
    relayUri.rawUserInfo == null && relayUri.rawQuery == null && relayUri.rawFragment == null &&
    relayUri.rawPath.isNullOrEmpty() && relayUri.port in -1..65535 && relayUri.port != 0) {
    "relayOrigin must be an HTTPS origin without credentials, path, query or fragment"
}

android {
    namespace = "com.aurora.wechatrelay.probe"
    compileSdk = 36

    buildFeatures {
        buildConfig = true
    }

    defaultConfig {
        applicationId = "com.aurora.wechatrelay.probe"
        minSdk = 28
        targetSdk = 36
        versionCode = 30
        versionName = "0.6.13"

        buildConfigField("String", "RELAY_ORIGIN", "\"$relayOrigin\"")

        buildConfigField("boolean", "LOCKSCREEN_ACCESSIBILITY_REPLY", "false")

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        debug {
            buildConfigField("boolean", "LOCKSCREEN_ACCESSIBILITY_REPLY", "true")
        }
        release {
            buildConfigField("boolean", "LOCKSCREEN_ACCESSIBILITY_REPLY", "false")
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    implementation("androidx.room:room-runtime:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")
    implementation("androidx.work:work-runtime:2.10.0")
}
