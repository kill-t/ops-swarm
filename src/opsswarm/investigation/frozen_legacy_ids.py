"""已发布冻结场景的旧身份目录；按完整新内容身份限定兼容范围。"""

# 由第 73 步场景的原 Pydantic JSON 与必需证据排列生成；运行时不枚举。
LEGACY_SNAPSHOT_IDS: dict[str, frozenset[str]] = {
    # database-pool-misconfiguration-v1
    "frozen-v2:dd48b15395110bd3898f1917af6db28928a440f08fe895fdd325e29a46b6533f": frozenset(
        {
            "frozen:2706313cf10b50b31f11f03758c5e661b5cab8e70c30f86e69fb061b30c4ddb3",
            "frozen:c9de9961daf36bef727b2fb89b7bb39b63457cb9f6944e6f398839542e81ca15",
        }
    ),
    # database-pool-misconfiguration-v2
    "frozen-v2:1d56ad7d0447e0f296fa6753408775a0231bd59fe41d14b367c3559a1adb9a67": frozenset(
        {
            "frozen:2c09638613156967c90ac19af90e73ae929cfab6a28fb8487a42ac7345338efe",
            "frozen:607930a6749be38e76734d5dd4fdb8d2eb3a1d7cb0703fb472749f3c2969eb5b",
        }
    ),
    # redis-unavailable-v1
    "frozen-v2:ea645be70e7d838c1dbbcafee04539f9b7316731f93090185892ecc276d87900": frozenset(
        {
            "frozen:b0d8c48afc18c72368af545cdc916572ef39f1666b1bd86646a0c48d4757f1f0",
            "frozen:f4c9d3e06b65b7f7310c31ac79cb7cf3733af057f3f1dcd4bcbba6d7bef042dc",
        }
    ),
    # redis-unavailable-v2
    "frozen-v2:31e9af2a5b9030a0a35141f4b66828da89b246e33966e93c0d7d3219d0b91ece": frozenset(
        {
            "frozen:049a33d1af3ab3b3ab210c790f3c2b58ae59263b93d8e0a81fb48bff0f6eba9d",
            "frozen:e19b95c6461c83f267ce3f9a73875c7cc5b3501dbe1cba01383a98d99db544ad",
        }
    ),
    # release-500-baseline-v1
    "frozen-v2:339ec5058f4156c0b6b02a1a0f7092485bfe9277d356595e0993d49e4d858e6b": frozenset(
        {
            "frozen:39736f267d5734719996cdf46bf49fcbb9107df39d4834d17af64fd2d83ad988",
            "frozen:5b5d8ebad6623dbe982b1ef2571245097eb493a4d0ab0a119e516f9c32209691",
            "frozen:5dc93949c49a3c257abe8dd7d174a76fdf61de343c5edde8b3b36ebbc3803924",
            "frozen:6a9f421c10f997ea78621b887a19f523a3c9a11786a6f1c85ca6a7c15309f5af",
            "frozen:85493133e33b2677833f1aa8c408c86acffb32f43350320593e0775575c438f7",
            "frozen:f6fc007678934cc97e765975f4e288f43e101a77a80f5d583ea6d4380dd07ff0",
        }
    ),
    # release-500-baseline-v2
    "frozen-v2:d7207c6f1348c0bc14abf148bd89c6742ba3c562063ea74f6f896ed1ba1d79d2": frozenset(
        {
            "frozen:0f41f4fb5de0c5256d8846f98d8fd9fee6b8697623e034ec501bfb3cc0cec4bf",
            "frozen:7d608a54b9c7864ecfe9c4dd7db8ff60f0984feb8e2cccaa643ba460748dc5a9",
            "frozen:821ab9d964d19330fee4f885ddd4f874682235d621e2f801a776cd960ae76581",
            "frozen:c632e324702ccc6dca80fa5e4f9b2a2cdfbce97b7d42bd4c0765e50cf5f2d492",
            "frozen:d3eb819c221fb94faa6cc8dd61690aefb4973754a93897677bf12baf14c0223f",
            "frozen:faaf275640b1d4a491697dab632ea9b5988c5e9bc5f25a34718c862f623eef9e",
        }
    ),
}
