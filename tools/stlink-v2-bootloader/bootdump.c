#include <stdint.h>
#include <string.h>

#include <libopencm3/cm3/scb.h>
#include <libopencm3/stm32/gpio.h>
#include <libopencm3/stm32/rcc.h>
#include <libopencm3/usb/cdc.h>
#include <libopencm3/usb/usbd.h>

#define BOOT_BASE ((const uint8_t *)0x08000000U)
#define BOOT_SIZE 0x4000U
#define HEADER_SIZE 16U

static usbd_device *usbdev;
static volatile uint8_t dump_requested;
static uint32_t stream_offset;
static uint32_t boot_crc;

static const struct usb_device_descriptor device_descriptor = {
	.bLength = USB_DT_DEVICE_SIZE,
	.bDescriptorType = USB_DT_DEVICE,
	.bcdUSB = 0x0200,
	.bDeviceClass = USB_CLASS_CDC,
	.bMaxPacketSize0 = 64,
	.idVendor = 0x1209,
	.idProduct = 0xF1D0,
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
	"ST-LINK Bootloader Dumper",
	"BOOTDUMP01",
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
	uint8_t command[64];
	(void)endpoint;
	int length = usbd_ep_read_packet(dev, 0x02, command, sizeof(command));
	for (int i = 0; i < length; ++i) {
		if (command[i] == 'D') {
			dump_requested = 1;
			stream_offset = 0;
		}
	}
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

static uint8_t stream_byte(uint32_t offset)
{
	static const uint8_t magic[8] = {'S','T','L','2','D','U','M','P'};
	if (offset < 8)
		return magic[offset];
	if (offset < 12)
		return (uint8_t)(BOOT_SIZE >> (8U * (offset - 8U)));
	if (offset < 16)
		return (uint8_t)(boot_crc >> (8U * (offset - 12U)));
	return BOOT_BASE[offset - HEADER_SIZE];
}

static void send_next_packet(void)
{
	uint8_t packet[64];
	const uint32_t total = HEADER_SIZE + BOOT_SIZE;
	if (!dump_requested || stream_offset >= total)
		return;
	uint32_t count = total - stream_offset;
	if (count > sizeof(packet))
		count = sizeof(packet);
	for (uint32_t i = 0; i < count; ++i)
		packet[i] = stream_byte(stream_offset + i);
	if (usbd_ep_write_packet(usbdev, 0x81, packet, count) == (int)count) {
		stream_offset += count;
		if (stream_offset == total)
			dump_requested = 0;
	}
}

int main(void)
{
	SCB_VTOR = 0x08004000U;
	/* Keep the loader clock tree; changing a live STM32F1 PLL is undefined. */
	rcc_periph_clock_enable(RCC_GPIOA);

	__asm__("cpsid i");

	boot_crc = crc32(BOOT_BASE, BOOT_SIZE);
	usbdev = usbd_init(&st_usbfs_v1_usb_driver, &device_descriptor,
		&config_descriptor, usb_strings, 3, control_buffer,
		sizeof(control_buffer));
	usbd_ep_setup(usbdev, 0x02, USB_ENDPOINT_ATTR_BULK, 64, data_received);
	usbd_ep_setup(usbdev, 0x81, USB_ENDPOINT_ATTR_BULK, 64, NULL);
	usbd_register_set_config_callback(usbdev, configured);

	while (1) {
		usbd_poll(usbdev);
		send_next_packet();
	}
}
