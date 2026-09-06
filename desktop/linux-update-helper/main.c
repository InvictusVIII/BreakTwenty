#include <gtk/gtk.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct {
  const char *ready_path;
  const char *probe_path;
  time_t timeout_at;
} MonitorState;

static const char *option_value(int argc, char **argv, const char *name) {
  int index;
  for (index = 1; index + 1 < argc; index += 2) {
    if (strcmp(argv[index], name) == 0) {
      return argv[index + 1];
    }
  }
  return NULL;
}

static int positive_integer_option(int argc, char **argv, const char *name) {
  const char *raw = option_value(argc, argv, name);
  char *end = NULL;
  long value;
  if (raw == NULL || *raw == '\0') {
    return 0;
  }
  value = strtol(raw, &end, 10);
  if (end == raw || *end != '\0' || value <= 0 || value > INT_MAX) {
    return 0;
  }
  return (int)value;
}

static void append_probe(const char *probe_path, const char *event_name) {
  FILE *file;
  if (probe_path == NULL || *probe_path == '\0') {
    return;
  }
  file = fopen(probe_path, "a");
  if (file == NULL) {
    return;
  }
  fprintf(file, "%s\n", event_name);
  fclose(file);
}

static GdkPixbuf *scaled_image(const char *path, int width, int height) {
  GError *error = NULL;
  GdkPixbuf *image = gdk_pixbuf_new_from_file_at_scale(path, width, height, TRUE, &error);
  if (error != NULL) {
    g_error_free(error);
  }
  return image;
}

static gboolean monitor_update(gpointer data) {
  MonitorState *state = (MonitorState *)data;
  gboolean ready = g_file_test(state->ready_path, G_FILE_TEST_EXISTS);
  if (ready || time(NULL) >= state->timeout_at) {
    append_probe(state->probe_path, ready ? "ready" : "timeout");
    gtk_main_quit();
    return G_SOURCE_REMOVE;
  }
  return G_SOURCE_CONTINUE;
}

static GtkWidget *centered_label(const char *text, const char *name) {
  GtkWidget *label = gtk_label_new(text);
  gtk_widget_set_name(label, name);
  gtk_label_set_justify(GTK_LABEL(label), GTK_JUSTIFY_CENTER);
  gtk_label_set_line_wrap(GTK_LABEL(label), TRUE);
  gtk_widget_set_halign(label, GTK_ALIGN_CENTER);
  return label;
}

int main(int argc, char **argv) {
  int delay_ms = positive_integer_option(argc, argv, "--delay-ms");
  int timeout_seconds = positive_integer_option(argc, argv, "--timeout-seconds");
  const char *ready_path = option_value(argc, argv, "--ready-path");
  const char *mark_path = option_value(argc, argv, "--mark-path");
  const char *wordmark_path = option_value(argc, argv, "--wordmark-path");
  const char *version = option_value(argc, argv, "--version");
  const char *probe_path = option_value(argc, argv, "--probe-path");
  const char *probe_mode = option_value(argc, argv, "--probe-mode");
  gboolean headless_probe = probe_mode != NULL && strcmp(probe_mode, "headless") == 0;
  gint64 show_at;
  GdkPixbuf *mark_pixbuf;
  GdkPixbuf *wordmark_pixbuf;
  GtkCssProvider *css_provider;
  GtkWidget *window;
  GtkWidget *layout;
  GtkWidget *mark;
  GtkWidget *wordmark;
  GtkWidget *title;
  GtkWidget *version_label = NULL;
  GtkWidget *body;
  GtkWidget *spinner;
  MonitorState monitor;

  if (
    delay_ms <= 0
    || timeout_seconds <= 0
    || ready_path == NULL
    || (!headless_probe && (mark_path == NULL || wordmark_path == NULL))
  ) {
    return 2;
  }
  append_probe(probe_path, "started");
  show_at = g_get_monotonic_time() + ((gint64)delay_ms * 1000);
  while (g_get_monotonic_time() < show_at) {
    if (g_file_test(ready_path, G_FILE_TEST_EXISTS)) {
      append_probe(probe_path, "suppressed-ready");
      return 0;
    }
    g_usleep(100000);
  }

  if (headless_probe) {
    gint64 timeout_at = g_get_monotonic_time() + ((gint64)timeout_seconds * G_USEC_PER_SEC);
    append_probe(probe_path, "shown");
    while (
      !g_file_test(ready_path, G_FILE_TEST_EXISTS)
      && g_get_monotonic_time() < timeout_at
    ) {
      g_usleep(100000);
    }
    append_probe(
      probe_path,
      g_file_test(ready_path, G_FILE_TEST_EXISTS) ? "ready" : "timeout"
    );
    return 0;
  }

  if (!gtk_init_check(&argc, &argv)) {
    return 3;
  }
  mark_pixbuf = scaled_image(mark_path, 64, 64);
  wordmark_pixbuf = scaled_image(wordmark_path, 160, 39);
  if (mark_pixbuf == NULL || wordmark_pixbuf == NULL) {
    if (mark_pixbuf != NULL) {
      g_object_unref(mark_pixbuf);
    }
    if (wordmark_pixbuf != NULL) {
      g_object_unref(wordmark_pixbuf);
    }
    return 4;
  }

  css_provider = gtk_css_provider_new();
  gtk_css_provider_load_from_data(
    css_provider,
    "window { background: #181817; }"
    "label { font-family: Inter, sans-serif; }"
    "#title { color: #ede7dd; font-size: 16px; font-weight: 700; }"
    "#version { color: #8e877b; font-size: 12px; }"
    "#body { color: #e8bc92; font-size: 13px; }"
    "spinner { color: #eb8d35; }",
    -1,
    NULL
  );
  gtk_style_context_add_provider_for_screen(
    gdk_screen_get_default(),
    GTK_STYLE_PROVIDER(css_provider),
    GTK_STYLE_PROVIDER_PRIORITY_APPLICATION
  );

  window = gtk_window_new(GTK_WINDOW_TOPLEVEL);
  gtk_window_set_title(GTK_WINDOW(window), "BreakTwenty is updating");
  gtk_window_set_default_size(GTK_WINDOW(window), 520, 390);
  gtk_window_set_position(GTK_WINDOW(window), GTK_WIN_POS_CENTER);
  gtk_window_set_resizable(GTK_WINDOW(window), FALSE);
  gtk_window_set_deletable(GTK_WINDOW(window), FALSE);
  gtk_window_set_keep_above(GTK_WINDOW(window), TRUE);
  gtk_window_set_icon_from_file(GTK_WINDOW(window), mark_path, NULL);

  layout = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
  gtk_widget_set_halign(layout, GTK_ALIGN_CENTER);
  gtk_widget_set_valign(layout, GTK_ALIGN_CENTER);
  gtk_container_add(GTK_CONTAINER(window), layout);

  mark = gtk_image_new_from_pixbuf(mark_pixbuf);
  gtk_box_pack_start(GTK_BOX(layout), mark, FALSE, FALSE, 0);
  gtk_widget_set_margin_bottom(mark, 10);
  wordmark = gtk_image_new_from_pixbuf(wordmark_pixbuf);
  gtk_box_pack_start(GTK_BOX(layout), wordmark, FALSE, FALSE, 0);
  gtk_widget_set_margin_bottom(wordmark, 22);

  title = centered_label("Please wait, BreakTwenty is updating", "title");
  gtk_box_pack_start(GTK_BOX(layout), title, FALSE, FALSE, 0);
  gtk_widget_set_margin_bottom(title, 8);
  if (version != NULL && *version != '\0') {
    char *version_text = g_strdup_printf("Updating to BreakTwenty %s", version);
    version_label = centered_label(version_text, "version");
    gtk_box_pack_start(GTK_BOX(layout), version_label, FALSE, FALSE, 0);
    gtk_widget_set_margin_bottom(version_label, 8);
    g_free(version_text);
  }
  body = centered_label(
    "BreakTwenty will reopen automatically when the update is finished.",
    "body"
  );
  gtk_box_pack_start(GTK_BOX(layout), body, FALSE, FALSE, 0);
  gtk_widget_set_margin_bottom(body, 24);
  spinner = gtk_spinner_new();
  gtk_widget_set_size_request(spinner, 30, 30);
  gtk_box_pack_start(GTK_BOX(layout), spinner, FALSE, FALSE, 0);
  gtk_spinner_start(GTK_SPINNER(spinner));

  monitor.ready_path = ready_path;
  monitor.probe_path = probe_path;
  monitor.timeout_at = time(NULL) + timeout_seconds;
  g_timeout_add(500, monitor_update, &monitor);
  gtk_widget_show_all(window);
  gtk_window_present(GTK_WINDOW(window));
  append_probe(probe_path, "shown");
  gtk_main();

  g_object_unref(mark_pixbuf);
  g_object_unref(wordmark_pixbuf);
  g_object_unref(css_provider);
  return 0;
}
