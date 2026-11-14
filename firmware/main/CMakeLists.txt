idf_component_register(
    SRCS "terminal_main.c"
         "terminal_ui.c"
         "terminal_link.c"
         "terminal_proto.c"
         "terminal_net.c"
         "terminal_audio.c"
         "terminal_auth.c"
         "terminal_capture.c"
         "terminal_playback.c"
         "terminal_pav1.c"
    INCLUDE_DIRS "."
    REQUIRES bsp
             esp_driver_usb_serial_jtag
             esp_timer
             json
             esp_wifi
             esp_netif
             esp_event
             nvs_flash
             lwip
             mbedtls
)