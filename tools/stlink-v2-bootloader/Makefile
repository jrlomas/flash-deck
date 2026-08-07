LIBOPENCM3 ?=
BUILD ?= build
CONFIG_DIR ?= $(BUILD)
CROSS ?= arm-none-eabi

ifeq ($(strip $(LIBOPENCM3)),)
$(error Set LIBOPENCM3 to a built libopencm3 checkout)
endif

CC := $(CROSS)-gcc
OBJCOPY := $(CROSS)-objcopy
CFLAGS := -Os -g3 -std=c11 -Wall -Wextra -Werror -DSTM32F1 \
	-mcpu=cortex-m3 -mthumb -ffunction-sections -fdata-sections \
	-I$(LIBOPENCM3)/include
LDFLAGS := -nostartfiles -T stlink-app.ld -L$(LIBOPENCM3)/lib \
	-Wl,--gc-sections -lopencm3_stm32f1 -lc -lnosys

.PHONY: all clean

all: $(BUILD)/bootdump.bin $(BUILD)/bootpatch.bin

$(BUILD):
	mkdir -p $@

$(BUILD)/bootdump.elf: bootdump.c stlink-app.ld | $(BUILD)
	$(CC) $(CFLAGS) $< $(LDFLAGS) -Wl,-Map=$(BUILD)/bootdump.map -o $@

$(BUILD)/bootpatch.elf: bootpatch.c stlink-app.ld $(CONFIG_DIR)/bootpatch_config.h | $(BUILD)
	$(CC) $(CFLAGS) -I$(CONFIG_DIR) $< $(LDFLAGS) -Wl,-Map=$(BUILD)/bootpatch.map -o $@

$(BUILD)/%.bin: $(BUILD)/%.elf
	$(OBJCOPY) -O binary $< $@

clean:
	rm -f $(BUILD)/bootdump.elf $(BUILD)/bootdump.bin $(BUILD)/bootdump.map
	rm -f $(BUILD)/bootpatch.elf $(BUILD)/bootpatch.bin $(BUILD)/bootpatch.map
