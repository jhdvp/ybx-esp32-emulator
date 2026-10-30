/* Host-only model test (no QEMU build needed): discrete state machine for
 * EpdBus::waitRefreshComplete (freeink-sdk EpdBus.cpp:388-468), the
 * epdBusyIsr semaphore give (EpdBus.cpp:76-81), the YBX SSD1677 QEMU device
 * busy pulse (hw/display/ybx_ssd1677.c:148-167: command 0x20 -> BUSY HIGH,
 * 350 ms virtual timer -> LOW) and three GPIO IRQ models:
 *   - "shipped": the binary that ran 0037b/0039 probes (busy output unwired
 *     to GPIO6, no STATUS/PCPU_INT, no GPIO line into the interrupt matrix);
 *   - "patch0042": hw/gpio/esp32_gpio.c as currently modified in
 *     qemu-rominit-20261029 (edge latches STATUS, IDF S3 ISR reads PCPU_INT
 *     0x05c and acks via STATUS_W1TC 0x04c, line deasserts when no enabled
 *     pin keeps STATUS set);
 *   - "patch0042_nopcpu": same wiring but PCPU_INT reads 0 — reproduces the
 *     quarantined 0020-0022 stuck-IRQ condition (unacked latched STATUS).
 * Tick = 1 ms (CONFIG_FREERTOS_HZ=1000 in the prebuilt Arduino SDK).
 * Build+run: cc -O1 -Wall -Wextra -o /tmp/epd_busy_test \
 *   tools/ybx-system-emu/experiments/0043-agent-epd-busy-model-test.c \
 *   && /tmp/epd_busy_test
 */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#define BUSY_PIN 6
#define REFRESH_MS 350        /* ybx_ssd1677.c: timer_mod(+350ms) */
#define TAKE_MS 20            /* EpdBus.cpp:445 xSemaphoreTake 20 ticks */
#define LONG_TAKE_MS 30000    /* EpdBus.cpp:460 pdMS_TO_TICKS(30000) @1kHz */
#define STORM_LIMIT 5000

typedef enum { MODE_SHIPPED, MODE_0042, MODE_0042_NOPCPU } GpioModel;

/* GPIO device subset (esp32_gpio.c + 0042 additions). */
typedef struct {
    bool busy_out_wired;    /* esp32s3.c: "ybx.epd.busy" -> "ybx.gpio-input"[6] */
    bool irq_to_matrix;     /* esp32s3.c: gpio.irq -> ETS_GPIO_INTR_SOURCE */
    bool expose_pcpu_int;   /* 0042: GPIO_PCPU_INT(0x05c) returns STATUS */
    uint32_t input_level;   /* GPIO_IN bit source for digitalRead() */
    uint32_t status;        /* STATUS lo word (pin6 = bit6) */
    uint32_t pin_cfg;       /* S3 pin reg: bits 7-9 int_type, bit 13 int_ena */
    bool irq_line;
    unsigned irq_deliveries;
    unsigned acks;
} Gpio;

static int cfg_type(Gpio *g) { return (int)((g->pin_cfg >> 7) & 7u); } /* 3=CHANGE */
static int cfg_ena(Gpio *g)  { return (int)((g->pin_cfg >> 13) & 1u); }

static void gpio_update_irq(Gpio *g)
{
    if (!g->irq_to_matrix) {
        g->irq_line = false;
        return;
    }
    g->irq_line = (g->status & (1u << BUSY_PIN)) && cfg_ena(g);
}

static void gpio_inject(Gpio *g, int level)
{
    uint32_t mask = 1u << BUSY_PIN;
    bool old = !!(g->input_level & mask);
    if (level) {
        g->input_level |= mask;
    } else {
        g->input_level &= ~mask;
    }
    bool edge = cfg_type(g) == 3 && old != (level != 0);
    if (edge && cfg_ena(g)) {
        g->status |= mask; /* latched until W1TC ack */
    }
    gpio_update_irq(g);
}

static void guest_ack(Gpio *g)
{
    g->status &= ~(1u << BUSY_PIN);
    g->acks++;
    gpio_update_irq(g);
}

/* Panel device: busy pulse armed on MASTER_ACTIVATION, cleared by timer. */
typedef struct { long busy_until_ms; bool arm; } Panel;

/* Guest: render task + ISR + binary semaphore token. */
typedef struct {
    Gpio *gpio;
    bool sem_token;
    bool waiting;
    long deadline;
    long wake_ms;
    bool short_wait; /* resolved via the 20 ms fallback take */
} Guest;

/* IDF gpio_intr_service: reads PCPU_INT; with no status bit it cannot ack or
 * dispatch (the 0020-0022 storm mechanism when STATUS is latched but
 * PCPU_INT is not modelled). */
static void isr_enter(Guest *g)
{
    Gpio *d = g->gpio;
    uint32_t status = d->expose_pcpu_int ? d->status : 0u;
    d->irq_deliveries++;
    if (!(status & (1u << BUSY_PIN))) {
        return; /* spurious/stuck: nothing acked, line still asserted */
    }
    g->sem_token = true; /* epdBusyIsr: xSemaphoreGiveFromISR */
    guest_ack(d);
}

/* EpdBus::waitRefreshComplete, BusyPolarity::ActiveHigh (SSD1677 X4 Pro). */
static void wait_refresh_complete(Guest *g, long now_ms)
{
    Gpio *d = g->gpio;
    g->sem_token = false;                       /* :437 drain take(0) */
    d->pin_cfg = (3u << 7) | (1u << 13);        /* :438 attachInterrupt CHANGE */
    gpio_update_irq(d);
    bool saw_working = !!(d->input_level & (1u << BUSY_PIN)); /* :440 */
    g->waiting = true;
    g->short_wait = !saw_working;
    g->deadline = now_ms + (saw_working ? LONG_TAKE_MS : TAKE_MS);
}

typedef struct { long wake_ms; unsigned irqs; unsigned acks; } Result;

static Result run(GpioModel model, long t0)
{
    Gpio d = {
        .busy_out_wired = model != MODE_SHIPPED,
        .irq_to_matrix = model != MODE_SHIPPED,
        .expose_pcpu_int = model == MODE_0042,
    };
    Guest g = { .gpio = &d };
    Panel p = { .arm = true };
    long t;
    for (t = t0; t < t0 + 60000; t++) {
        if (p.arm) { /* command 0x20 handled synchronously inside the SPI byte */
            p.arm = false;
            p.busy_until_ms = t + REFRESH_MS;
            if (d.busy_out_wired) {
                gpio_inject(&d, 1); /* busy HIGH; ENA still 0 -> no edge latch */
            }
            wait_refresh_complete(&g, t);
        }
        if (d.irq_line) {
            isr_enter(&g);
            if (d.irq_deliveries > STORM_LIMIT) {
                break;
            }
        }
        if (g.waiting && t >= g.deadline) { /* tick expired the take */
            g.waiting = false;
            if (g.short_wait) {
                g.wake_ms = t; /* detach + return (self-heal path) */
                break;
            }
            g.deadline = t + LONG_TAKE_MS; /* stay blocked */
        }
        if (g.waiting && g.sem_token) { /* ISR woke xSemaphoreTake */
            g.sem_token = false;
            if (!(d.input_level & (1u << BUSY_PIN))) { /* :459 done level */
                g.waiting = false;
                g.wake_ms = t;
                break;
            }
            g.deadline = t + LONG_TAKE_MS;
        }
        if (p.busy_until_ms && t == p.busy_until_ms) { /* 350 ms timer */
            p.busy_until_ms = 0;
            if (d.busy_out_wired) {
                gpio_inject(&d, 0); /* falling edge -> latched STATUS */
            }
        }
    }
    Result r = { g.wake_ms, d.irq_deliveries, d.acks };
    return r;
}

int main(void)
{
    Result r;

    /* Shipped binary: GPIO6 never rises; the wait falls through to the 20 ms
     * fallback take and self-heals. No IRQ, no infinite EPD hang: a
     * multi-second render freeze seen in GDB probes cannot be explained by
     * this wait alone. */
    r = run(MODE_SHIPPED, 0);
    printf("shipped:        wake=%ld irqs=%u acks=%u\n", r.wake_ms, r.irqs, r.acks);
    assert(r.wake_ms == TAKE_MS && r.irqs == 0);

    /* 0042 as applied in qemu-rominit-20261029: the rising edge happens while
     * the ISR is not yet attached (ENA=0 -> correctly ignored, no storm), and
     * the task sleeps through the whole waveform, waking on the real falling
     * edge (t=350 + 1 tick); the single edge IRQ was acked and the line is
     * deasserted afterwards. */
    r = run(MODE_0042, 0);
    printf("patch 0042:     wake=%ld irqs=%u acks=%u\n", r.wake_ms, r.irqs, r.acks);
    assert(r.wake_ms == REFRESH_MS + 1 && r.irqs == 1 && r.acks == 1);

    /* 0042 minus PCPU_INT exposure: latched STATUS raises the line forever —
     * exactly the quarantined 0020-0022 storm; do not ship without
     * GPIO_PCPU_INT + W1TC ack. */
    r = run(MODE_0042_NOPCPU, 0);
    printf("patch no-pcpu:  wake=%ld irqs=%u acks=%u (storm)\n",
           r.wake_ms, r.irqs, r.acks);
    assert(r.wake_ms == 0 && r.irqs > STORM_LIMIT && r.acks == 0);

    printf("PASS: EPD busy model verified; PCPU_INT+ack is the storm guard\n");
    return 0;
}
