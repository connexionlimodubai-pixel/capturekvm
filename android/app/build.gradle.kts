plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.capturekvm"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.capturekvm"
        // UVCAndroid needs USB host support; 26 is the floor its prebuilt library supports.
        minSdk = 26
        targetSdk = 35
        versionCode = 2
        versionName = "0.2.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }

    packaging {
        resources.excludes += setOf("META-INF/DEPENDENCIES", "META-INF/LICENSE*", "META-INF/NOTICE*")
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    // The USB Video Class stack: enumeration, permission, format choice and a preview surface,
    // with prebuilt libuvc/libjpeg .so files for all four ABIs.
    implementation("com.herohan:UVCAndroid:1.0.13")

    // X25519 and ChaCha20-Poly1305. Android's own XDH KeyAgreement is API 33+ and its
    // ChaCha20-Poly1305 cipher is API 28+, so the link would not work on the phones this app
    // targets without a provider of its own.
    implementation("org.bouncycastle:bcprov-jdk18on:1.78.1")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.bouncycastle:bcprov-jdk18on:1.78.1")
    // Android's org.json is a stub under unit tests; the real one keeps the wire tests honest.
    testImplementation("org.json:json:20240303")
}
