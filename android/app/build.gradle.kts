plugins {
    id("com.android.application")
}

configurations.configureEach {
    exclude(group = "xpp3", module = "xpp3")
}

android {
    namespace = "org.example.quark.android"
    compileSdk = 35

    defaultConfig {
        applicationId = "org.example.quark.android"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.3.1"
    }

    signingConfigs {
        create("releaseKey") {
            val keyFile = System.getenv("QUARK_ANDROID_KEYSTORE")
            if (!keyFile.isNullOrBlank()) {
                storeFile = file(keyFile)
                storePassword = System.getenv("QUARK_ANDROID_STORE_PASSWORD")
                keyAlias = System.getenv("QUARK_ANDROID_KEY_ALIAS") ?: "quark"
                keyPassword = System.getenv("QUARK_ANDROID_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        getByName("release") {
            isMinifyEnabled = false
            if (!System.getenv("QUARK_ANDROID_KEYSTORE").isNullOrBlank()) {
                signingConfig = signingConfigs.getByName("releaseKey")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    val smack = "4.4.8"
    implementation("org.igniterealtime.smack:smack-android:$smack")
    implementation("org.igniterealtime.smack:smack-tcp:$smack")
    implementation("org.igniterealtime.smack:smack-im:$smack")
    implementation("org.igniterealtime.smack:smack-extensions:$smack")
    implementation("org.igniterealtime.smack:smack-experimental:$smack")
}
