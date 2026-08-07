#include <stdint.h>
#include <string.h>

#include <libopencm3/cm3/scb.h>
#include <libopencm3/stm32/flash.h>
#include <libopencm3/stm32/gpio.h>
#include <libopencm3/stm32/rcc.h>
#include <libopencm3/usb/cdc.h>
#include <libopencm3/usb/usbd.h>

#include "bootpatch_config.h"

#define BOOT_BASE_ADDR 0x08000000U
#define BOOT_BASE ((const uint8_t *)BOOT_BASE_ADDR)
#define BOOT_SIZE 0x4000U
#define PAGE_SIZE 0x400U
#define DESCRIPTOR_PAGE 0x08002800U
#define FORMATTER_PAGE 0x08001000U

enum patch_status {
	PATCH_IDLE = 0,
	PATCH_BAD_COMMAND = 1,
	PATCH_BAD_ORIGINAL_CRC = 2,
	PATCH_BAD_DESCRIPTOR_ANCHOR = 3,
	PATCH_BAD_FORMATTER_ANCHOR = 4,
	PATCH_FLASH_UNLOCK_FAILED = 5,
	PATCH_DESCRIPTOR_ERASE_FAILED = 6,
	PATCH_DESCRIPTOR_PROGRAM_FAILED = 7,
	PATCH_DESCRIPTOR_VERIFY_FAILED = 8,
	PATCH_FORMATTER_ERASE_FAILED = 9,
	PATCH_FORMATTER_PROGRAM_FAILED = 10,
	PATCH_FORMATTER_VERIFY_FAILED = 11,
	PATCH_BAD_FINAL_CRC = 12,
	PATCH_WRITE_PROTECTED = 13,
	PATCH_SUCCESS = 0x5a,
	PATCH_ALREADY_PATCHED = 0x5b,
};

struct patch_reply {
	uint8_t magic[8];
	uint32_t status;
	uint32_t before_crc;
	uint32_t after_crc;
	uint32_t flash_sr;
	uint32_t expected_after_crc;
	uint32_t flash_wrpr;
	uint32_t device_id;
	uint32_t flash_kib;
} __attribute__((packed));

static usbd_device *usbdev;
static volatile uint8_t preflight_requested;
static volatile uint8_t patch_requested;
static volatile uint8_t reply_pending;
static uint8_t page_buffer[PAGE_SIZE] __attribute__((aligned(4)));
static struct patch_reply reply = {
	.magic = {'F','D','S','P','A','T','C','H'},
	.status = PATCH_IDLE,
	.expected_after_crc = PATCHED_CRC,
};

static const uint8_t formatter_anchor[] = {
	0x10, 0xb4, 0x93, 0x48, 0x01, 0x68, 0x42, 0x68,
	0x80, 0x68, 0xd9, 0xb1,
};

static const uint8_t original_descriptor[] = {
	0x1a, 0x03,
	'0', 0, '0', 0, '0', 0, '0', 0, '0', 0, '0', 0,
	'0', 0, '0', 0, '0', 0, '0', 0, '0', 0, '1', 0,
};

static const uint8_t patched_descriptor[] = {
	PATCHED_DESCRIPTOR_BYTES
};

static const struct usb_device_descriptor device_descriptor = {
	.bLength = USB_DT_DEVICE_SIZE,
	.bDescriptorType = USB_DT_DEVICE,
	.bcdUSB = 0x0200,
	.bDeviceClass = USB_CLASS_CDC,
	.bMaxPacketSize0 = 64,
	.idVendor = 0x1209,
	.idProduct = 0xF1D1,
	.bcdDevice = 0x0100,
	.iManufacturer = 1,
	.iProduct = 2,
	.iSerialNumber = 3,
	.bNumConfigurations = 1,
};

static const struct usb_endpoint_descriptor comm_endpoints[] = {{
	.bLength = USB_DT_ENDPOINT_SIZE,
	.bDescriptorType = USB_DT_ENDPOINT,
	.bEndpointAddress = 0x83,
	.bmAttributes = USB_ENDPOINT_ATTR_INTERRUPT,
	.wMaxPacketSize = 16,
	.bInterval = 255,
}};

static const struct usb_endpoint_descriptor data_endpoints[] = {{
	.bLength = USB_DT_ENDPOINT_SIZE,
	.bDescriptorType = USB_DT_ENDPOINT,
	.bEndpointAddress = 0x02,
	.bmAttributes = USB_ENDPOINT_ATTR_BULK,
	.wMaxPacketSize = 64,
	.bInterval = 1,
}, {
	.bLength = USB_DT_ENDPOINT_SIZE,
	.bDescriptorType = USB_DT_ENDPOINT,
	.bEndpointAddress = 0x81,
	.bmAttributes = USB_ENDPOINT_ATTR_BULK,
	.wMaxPacketSize = 64,
	.bInterval = 1,
}};

static const struct {
	struct usb_cdc_header_descriptor header;
	struct usb_cdc_call_management_descriptor call_mgmt;
	struct usb_cdc_acm_descriptor acm;
	struct usb_cdc_union_descriptor cdc_union;
} __attribute__((packed)) cdc_descriptors = {
	.header = {
		.bFunctionLength = sizeof(struct usb_cdc_header_descriptor),
		.bDescriptorType = CS_INTERFACE,
		.bDescriptorSubtype = USB_CDC_TYPE_HEADER,
		.bcdCDC = 0x0110,
	},
	.call_mgmt = {
		.bFunctionLength = sizeof(struct usb_cdc_call_management_descriptor),
		.bDescriptorType = CS_INTERFACE,
		.bDescriptorSubtype = USB_CDC_TYPE_CALL_MANAGEMENT,
		.bDataInterface = 1,
	},
	.acm = {
		.bFunctionLength = sizeof(struct usb_cdc_acm_descriptor),
		.bDescriptorType = CS_INTERFACE,
		.bDescriptorSubtype = USB_CDC_TYPE_ACM,
	},
	.cdc_union = {
		.bFunctionLength = sizeof(struct usb_cdc_union_descriptor),
		.bDescriptorType = CS_INTERFACE,
		.bDescriptorSubtype = USB_CDC_TYPE_UNION,
		.bControlInterface = 0,
		.bSubordinateInterface0 = 1,
	},
};

static const struct usb_interface_descriptor comm_interface[] = {{
	.bLength = USB_DT_INTERFACE_SIZE,
	.bDescriptorType = USB_DT_INTERFACE,
	.bInterfaceNumber = 0,
	.bNumEndpoints = 1,
	.bInterfaceClass = USB_CLASS_CDC,
	.bInterfaceSubClass = USB_CDC_SUBCLASS_ACM,
	.bInterfaceProtocol = USB_CDC_PROTOCOL_AT,
	.endpoint = comm_endpoints,
	.extra = &cdc_descriptors,
	.extralen = sizeof(cdc_descriptors),
}};

static const struct usb_interface_descriptor data_interface[] = {{
	.bLength = USB_DT_INTERFACE_SIZE,
	.bDescriptorType = USB_DT_INTERFACE,
	.bInterfaceNumber = 1,
	.bNumEndpoints = 2,
	.bInterfaceClass = USB_CLASS_DATA,
	.endpoint = data_endpoints,
}};

static const struct usb_interface interfaces[] = {{
	.num_altsetting = 1,
	.altsetting = comm_interface,
}, {
	.num_altsetting = 1,
	.altsetting = data_interface,
}};

static const struct usb_config_descriptor config_descriptor = {
	.bLength = USB_DT_CONFIGURATION_SIZE,
	.bDescriptorType = USB_DT_CONFIGURATION,
	.bNumInterfaces = 2,
	.bConfigurationValue = 1,
	.bmAttributes = 0x80,
	.bMaxPower = 0x32,
	.interface = interfaces,
};

static const char *usb_strings[] = {
	"Flash Deck",
	"ST-LINK Bootloader Serial Patcher",
	"BOOTPATCH01",
};

static uint8_t control_buffer[128];

static uint32_t crc32(const uint8_t *data, uint32_t length)
{
	uint32_t crc = 0xffffffffU;
	for (uint32_t i = 0; i < length; ++i) {
		crc ^= data[i];
		for (unsigned bit = 0; bit < 8; ++bit)
			crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
	}
	return ~crc;
}

static enum usbd_request_return_codes control_request(
	usbd_device *dev, struct usb_setup_data *req, uint8_t **buf,
	uint16_t *len, void (**complete)(usbd_device *, struct usb_setup_data *))
{
	(void)dev;
	(void)buf;
	(void)complete;
	if (req->bRequest == USB_CDC_REQ_SET_CONTROL_LINE_STATE)
		return USBD_REQ_HANDLED;
	if (req->bRequest == USB_CDC_REQ_SET_LINE_CODING &&
	    *len >= sizeof(struct usb_cdc_line_coding))
		return USBD_REQ_HANDLED;
	return USBD_REQ_NOTSUPP;
}

static void data_received(usbd_device *dev, uint8_t endpoint)
{
	static const uint8_t patch_command[16] = {
		'P','A','T','C','H','B','O','O',
		'T',':','F','1','A','5','!','!'
	};
	static const uint8_t preflight_command[16] = {
		'P','R','E','F','L','I','G','H',
		'T',':','F','1','A','5','!','!'
	};
	uint8_t command[64];
	(void)endpoint;
	int length = usbd_ep_read_packet(dev, 0x02, command, sizeof(command));
	if (length == (int)sizeof(patch_command) &&
	    memcmp(command, patch_command, sizeof(patch_command)) == 0) {
		patch_requested = 1;
	} else if (length == (int)sizeof(preflight_command) &&
		   memcmp(command, preflight_command,
		          sizeof(preflight_command)) == 0) {
		preflight_requested = 1;
	} else {
		reply.status = PATCH_BAD_COMMAND;
		reply_pending = 1;
	}
}

static void fill_preflight(void)
{
	static const uint8_t formatter_patch[] = {0x70, 0x47, 0x00, 0xbf};
	reply.before_crc = crc32(BOOT_BASE, BOOT_SIZE);
	reply.after_crc = reply.before_crc;
	reply.flash_sr = FLASH_SR;
	reply.flash_wrpr = FLASH_WRPR;
	reply.device_id = *(const volatile uint32_t *)0xe0042000U;
	reply.flash_kib = *(const volatile uint16_t *)0x1ffff7e0U;
	reply.status = PATCH_IDLE;
	if (reply.before_crc == PATCHED_CRC) {
		if (memcmp(BOOT_BASE + 0x2a40U, patched_descriptor,
			   sizeof(patched_descriptor)) == 0 &&
		    memcmp(BOOT_BASE + 0x11e0U, formatter_patch,
			   sizeof(formatter_patch)) == 0)
			reply.status = PATCH_ALREADY_PATCHED;
		else
			reply.status = PATCH_BAD_FINAL_CRC;
	} else if (reply.before_crc != ORIGINAL_CRC)
		reply.status = PATCH_BAD_ORIGINAL_CRC;
	else if (memcmp(BOOT_BASE + 0x2a40U, original_descriptor,
			sizeof(original_descriptor)) != 0)
		reply.status = PATCH_BAD_DESCRIPTOR_ANCHOR;
	else if (memcmp(BOOT_BASE + 0x11e0U, formatter_anchor,
			sizeof(formatter_anchor)) != 0)
		reply.status = PATCH_BAD_FORMATTER_ANCHOR;
	else if (reply.flash_wrpr != 0xffffffffU)
		reply.status = PATCH_WRITE_PROTECTED;
}

static void configured(usbd_device *dev, uint16_t value)
{
	(void)value;
	usbd_ep_setup(dev, 0x02, USB_ENDPOINT_ATTR_BULK, 64, data_received);
	usbd_ep_setup(dev, 0x81, USB_ENDPOINT_ATTR_BULK, 64, NULL);
	usbd_ep_setup(dev, 0x83, USB_ENDPOINT_ATTR_INTERRUPT, 16, NULL);
	usbd_register_control_callback(dev,
		USB_REQ_TYPE_CLASS | USB_REQ_TYPE_INTERFACE,
		USB_REQ_TYPE_TYPE | USB_REQ_TYPE_RECIPIENT, control_request);
}

static uint32_t program_page(uint32_t page_address, uint32_t patch_offset,
	const uint8_t *patch, uint32_t patch_length,
	uint32_t erase_error, uint32_t program_error, uint32_t verify_error)
{
	for (uint32_t i = 0; i < PAGE_SIZE; ++i)
		page_buffer[i] = *(const volatile uint8_t *)(page_address + i);
	memcpy(page_buffer + patch_offset, patch, patch_length);

	flash_clear_status_flags();
	flash_erase_page(page_address);
	if (flash_get_status_flags() & (FLASH_SR_PGERR | FLASH_SR_WRPRTERR))
		return erase_error;

	for (uint32_t i = 0; i < PAGE_SIZE; i += 2) {
		uint16_t value = (uint16_t)page_buffer[i] |
			((uint16_t)page_buffer[i + 1] << 8);
		if (value != 0xffffU)
			flash_program_half_word(page_address + i, value);
		if (flash_get_status_flags() & (FLASH_SR_PGERR | FLASH_SR_WRPRTERR))
			return program_error;
	}

	if (memcmp((const void *)page_address, page_buffer, PAGE_SIZE) != 0)
		return verify_error;
	return PATCH_IDLE;
}

static void apply_bootloader_patch(void)
{
	static const uint8_t formatter_patch[] = {0x70, 0x47, 0x00, 0xbf};
	uint32_t status;

	fill_preflight();
	if (reply.status == PATCH_ALREADY_PATCHED)
		return;
	if (reply.status != PATCH_IDLE)
		return;

	flash_unlock();
	if (FLASH_CR & FLASH_CR_LOCK) {
		reply.status = PATCH_FLASH_UNLOCK_FAILED;
		return;
	}

	/* Inert data first; behavior changes only after the formatter patch. */
	status = program_page(DESCRIPTOR_PAGE, 0x240U, patched_descriptor,
			      sizeof(patched_descriptor),
			      PATCH_DESCRIPTOR_ERASE_FAILED,
			      PATCH_DESCRIPTOR_PROGRAM_FAILED,
			      PATCH_DESCRIPTOR_VERIFY_FAILED);
	if (status != PATCH_IDLE) {
		reply.status = status;
		goto done;
	}

	/* Commit last: retain the static descriptor instead of the bogus UID. */
	status = program_page(FORMATTER_PAGE, 0x1e0U, formatter_patch,
			      sizeof(formatter_patch),
			      PATCH_FORMATTER_ERASE_FAILED,
			      PATCH_FORMATTER_PROGRAM_FAILED,
			      PATCH_FORMATTER_VERIFY_FAILED);
	if (status != PATCH_IDLE) {
		reply.status = status;
		goto done;
	}

	reply.after_crc = crc32(BOOT_BASE, BOOT_SIZE);
	reply.status = reply.after_crc == PATCHED_CRC ?
		PATCH_SUCCESS : PATCH_BAD_FINAL_CRC;

done:
	reply.flash_sr = FLASH_SR;
	flash_lock();
}

int main(void)
{
	SCB_VTOR = 0x08004000U;
	/* Keep the loader clock tree; changing a live STM32F1 PLL is undefined. */
	rcc_periph_clock_enable(RCC_GPIOA);

	__asm__("cpsid i");

	usbdev = usbd_init(&st_usbfs_v1_usb_driver, &device_descriptor,
		&config_descriptor, usb_strings, 3, control_buffer,
		sizeof(control_buffer));
	usbd_ep_setup(usbdev, 0x02, USB_ENDPOINT_ATTR_BULK, 64, data_received);
	usbd_ep_setup(usbdev, 0x81, USB_ENDPOINT_ATTR_BULK, 64, NULL);
	usbd_register_set_config_callback(usbdev, configured);

	while (1) {
		usbd_poll(usbdev);
		if (preflight_requested) {
			preflight_requested = 0;
			fill_preflight();
			reply_pending = 1;
		}
		if (patch_requested) {
			patch_requested = 0;
			apply_bootloader_patch();
			reply_pending = 1;
		}
		if (reply_pending &&
		    usbd_ep_write_packet(usbdev, 0x81, &reply, sizeof(reply)) ==
		    (int)sizeof(reply))
			reply_pending = 0;
	}
}
