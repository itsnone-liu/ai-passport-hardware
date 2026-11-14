# Thin-terminal firmware sources

These are the sources of the `dsh terminal` firmware described in
[`../docs/terminal-firmware.md`](../docs/terminal-firmware.md), kept here so the work is
reproducible without the build machine.

They are a **snapshot of `main/` plus the project-level build files** of an ESP-IDF project
based on the vendor repository (`FoloToy/ai-passport-xiaozhi`). `components/bsp/` is vendor
code and is not duplicated here — take it from upstream unchanged.

## Dropping these into a project

```
ai-passport/
  CMakeLists.txt           <- firmware/CMakeLists.txt
  partitions.csv           <- firmware/partitions.csv
  sdkconfig.defaults       <- firmware/sdkconfig.defaults
  main/
    CMakeLists.txt         <- firmware/main/CMakeLists.txt
    Kconfig.projbuild      <- firmware/main/Kconfig.projbuild
    terminal_*.c/.h        <- firmware/main/
  components/bsp/          <- vendor BSP, unchanged
```

Delete the vendor demo sources from `main/` (`main.c`, `demo_*.c`) — `main/CMakeLists.txt`
here compiles only the `terminal_*` files.

The project name must stay `FoloToy-AI-Passport`: `corpus.toml` for the emulator keys off it.

## Notes

* `sdkconfig.defaults` targets esp32c3, 8 MB flash, the single-slot partition table, USB-Serial/JTAG
  console, LVGL 16-bit with Montserrat 14/20, and `BT_ENABLED=n`.
* Network defaults live in `main/Kconfig.projbuild`; they are only first-boot defaults, after
  which NVS wins (see `set_net` in the docs).
* None of these files contain deployment credentials.