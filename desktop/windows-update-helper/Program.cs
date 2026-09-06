using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

namespace BreakTwenty.UpdateHelper
{
    internal static class Program
    {
        private const int ProcessPollMilliseconds = 100;

        [STAThread]
        private static void Main(string[] args)
        {
            string probePath = String.Empty;
            try
            {
                Dictionary<string, string> options = ParseArguments(args);
                int delayMilliseconds = ParsePositiveInteger(options, "--delay-ms");
                int timeoutSeconds = ParsePositiveInteger(options, "--timeout-seconds");
                string readyPath = RequiredOption(options, "--ready-path");
                string version = OptionalOption(options, "--version");
                probePath = OptionalOption(options, "--probe-path");
                bool headlessProbe = String.Equals(
                    OptionalOption(options, "--probe-mode"),
                    "headless",
                    StringComparison.OrdinalIgnoreCase);

                AppendProbe(probePath, "started");
                DateTime showAt = DateTime.UtcNow.AddMilliseconds(delayMilliseconds);
                while (DateTime.UtcNow < showAt)
                {
                    if (File.Exists(readyPath))
                    {
                        AppendProbe(probePath, "suppressed-ready");
                        return;
                    }
                    Thread.Sleep(ProcessPollMilliseconds);
                }

                if (headlessProbe)
                {
                    AppendProbe(probePath, "shown");
                    DateTime timeoutAt = DateTime.UtcNow.AddSeconds(timeoutSeconds);
                    while (!File.Exists(readyPath) && DateTime.UtcNow < timeoutAt)
                    {
                        Thread.Sleep(ProcessPollMilliseconds);
                    }
                    AppendProbe(probePath, File.Exists(readyPath) ? "ready" : "timeout");
                    return;
                }

                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new UpdateProgressForm(readyPath, version, timeoutSeconds, probePath));
            }
            catch (Exception error)
            {
                AppendProbe(probePath, "error-" + error.GetType().Name);
            }
        }

        private static Dictionary<string, string> ParseArguments(string[] args)
        {
            Dictionary<string, string> options = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (int index = 0; index + 1 < args.Length; index += 2)
            {
                options[args[index]] = args[index + 1];
            }
            return options;
        }

        private static string RequiredOption(Dictionary<string, string> options, string name)
        {
            string value;
            if (!options.TryGetValue(name, out value) || String.IsNullOrWhiteSpace(value))
            {
                throw new ArgumentException("Missing required helper option.");
            }
            return value;
        }

        private static string OptionalOption(Dictionary<string, string> options, string name)
        {
            string value;
            return options.TryGetValue(name, out value) ? value : String.Empty;
        }

        private static int ParsePositiveInteger(Dictionary<string, string> options, string name)
        {
            int value;
            if (!Int32.TryParse(RequiredOption(options, name), out value) || value <= 0)
            {
                throw new ArgumentException("Invalid numeric helper option.");
            }
            return value;
        }

        internal static void AppendProbe(string probePath, string eventName)
        {
            if (String.IsNullOrWhiteSpace(probePath))
            {
                return;
            }
            try
            {
                File.AppendAllText(probePath, eventName + Environment.NewLine);
            }
            catch
            {
            }
        }

    }

    internal sealed class UpdateProgressForm : Form
    {
        private readonly string readyPath;
        private readonly string probePath;
        private readonly DateTime timeoutAt;
        private readonly System.Windows.Forms.Timer monitorTimer;

        internal UpdateProgressForm(string readyPath, string version, int timeoutSeconds, string probePath)
        {
            this.readyPath = readyPath;
            this.probePath = probePath;
            timeoutAt = DateTime.UtcNow.AddSeconds(timeoutSeconds);

            Text = "BreakTwenty is updating";
            ClientSize = new Size(520, 390);
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            ControlBox = false;
            TopMost = true;
            ShowInTaskbar = true;
            BackColor = Color.FromArgb(24, 24, 23);
            AutoScaleMode = AutoScaleMode.Dpi;

            try
            {
                Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            }
            catch
            {
            }

            Controls.Add(new BrandImageControl(
                LoadEmbeddedImage("BreakTwenty.UpdateHelper.Mark.png"),
                new Rectangle(228, 18, 64, 64)));
            Controls.Add(new BrandImageControl(
                LoadEmbeddedImage("BreakTwenty.UpdateHelper.Wordmark.png"),
                new Rectangle(180, 94, 160, 39)));
            Controls.Add(CreateLabel(
                "Please wait, BreakTwenty is updating",
                new Rectangle(0, 151, 520, 32),
                new Font("Segoe UI", 12, FontStyle.Bold),
                Color.FromArgb(237, 231, 221),
                ContentAlignment.MiddleCenter));

            if (!String.IsNullOrWhiteSpace(version))
            {
                Controls.Add(CreateLabel(
                    "Updating to BreakTwenty " + version,
                    new Rectangle(0, 185, 520, 24),
                    new Font("Segoe UI", 8.5f, FontStyle.Regular),
                    Color.FromArgb(142, 135, 123),
                    ContentAlignment.MiddleCenter));
            }

            Controls.Add(CreateLabel(
                "BreakTwenty will reopen automatically when the update is finished.",
                new Rectangle(65, 218, 390, 48),
                new Font("Segoe UI", 9, FontStyle.Regular),
                Color.FromArgb(232, 188, 146),
                ContentAlignment.TopCenter));

            SpinnerControl spinner = new SpinnerControl();
            spinner.Location = new Point(245, 306);
            spinner.Size = new Size(30, 30);
            Controls.Add(spinner);

            monitorTimer = new System.Windows.Forms.Timer();
            monitorTimer.Interval = 500;
            monitorTimer.Tick += MonitorUpdate;
            Shown += WindowShown;
            FormClosed += WindowClosed;
        }

        private static Image LoadEmbeddedImage(string resourceName)
        {
            using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(resourceName))
            {
                if (stream == null)
                {
                    throw new InvalidOperationException("A packaged BreakTwenty brand asset is missing.");
                }
                using (Image source = Image.FromStream(stream))
                {
                    return new Bitmap(source);
                }
            }
        }

        private static Label CreateLabel(
            string text,
            Rectangle bounds,
            Font font,
            Color color,
            ContentAlignment alignment)
        {
            Label label = new Label();
            label.Text = text;
            label.Bounds = bounds;
            label.Font = font;
            label.ForeColor = color;
            label.BackColor = Color.Transparent;
            label.TextAlign = alignment;
            label.AutoSize = false;
            return label;
        }

        private void WindowShown(object sender, EventArgs eventArgs)
        {
            Program.AppendProbe(probePath, "shown");
            monitorTimer.Start();
            Activate();
            BringToFront();
        }

        private void WindowClosed(object sender, FormClosedEventArgs eventArgs)
        {
            monitorTimer.Stop();
            monitorTimer.Dispose();
        }

        private void MonitorUpdate(object sender, EventArgs eventArgs)
        {
            bool ready = File.Exists(readyPath);
            if (ready || DateTime.UtcNow >= timeoutAt)
            {
                Program.AppendProbe(probePath, ready ? "ready" : "timeout");
                Close();
            }
        }
    }

    internal sealed class BrandImageControl : Control
    {
        private readonly Image image;

        internal BrandImageControl(Image image, Rectangle bounds)
        {
            this.image = image;
            Bounds = bounds;
            SetStyle(
                ControlStyles.AllPaintingInWmPaint
                | ControlStyles.OptimizedDoubleBuffer
                | ControlStyles.SupportsTransparentBackColor
                | ControlStyles.UserPaint,
                true);
            BackColor = Color.Transparent;
        }

        protected override void OnPaint(PaintEventArgs eventArgs)
        {
            base.OnPaint(eventArgs);
            eventArgs.Graphics.CompositingQuality = CompositingQuality.HighQuality;
            eventArgs.Graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
            eventArgs.Graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
            float scale = Math.Min(
                (float)ClientSize.Width / image.Width,
                (float)ClientSize.Height / image.Height);
            float width = image.Width * scale;
            float height = image.Height * scale;
            RectangleF target = new RectangleF(
                (ClientSize.Width - width) / 2.0f,
                (ClientSize.Height - height) / 2.0f,
                width,
                height);
            eventArgs.Graphics.DrawImage(image, target);
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                image.Dispose();
            }
            base.Dispose(disposing);
        }
    }

    internal sealed class SpinnerControl : Control
    {
        private readonly System.Windows.Forms.Timer animationTimer;
        private readonly Stopwatch rotationClock;

        internal SpinnerControl()
        {
            SetStyle(
                ControlStyles.AllPaintingInWmPaint
                | ControlStyles.OptimizedDoubleBuffer
                | ControlStyles.ResizeRedraw
                | ControlStyles.SupportsTransparentBackColor
                | ControlStyles.UserPaint,
                true);
            BackColor = Color.Transparent;
            rotationClock = Stopwatch.StartNew();
            animationTimer = new System.Windows.Forms.Timer();
            animationTimer.Interval = 16;
            animationTimer.Tick += RefreshSpinner;
            animationTimer.Start();
        }

        private void RefreshSpinner(object sender, EventArgs eventArgs)
        {
            Invalidate();
        }

        protected override void OnPaint(PaintEventArgs eventArgs)
        {
            base.OnPaint(eventArgs);
            eventArgs.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            float angle = (float)((rotationClock.Elapsed.TotalMilliseconds % 900.0) * 360.0 / 900.0);
            Rectangle arcBounds = new Rectangle(3, 3, 24, 24);
            using (Pen basePen = new Pen(Color.FromArgb(41, 255, 255, 255), 2.25f))
            using (Pen accentPen = new Pen(Color.FromArgb(235, 141, 53), 2.25f))
            {
                basePen.StartCap = LineCap.Round;
                basePen.EndCap = LineCap.Round;
                accentPen.StartCap = LineCap.Round;
                accentPen.EndCap = LineCap.Round;
                eventArgs.Graphics.DrawArc(basePen, arcBounds, 0, 360);
                eventArgs.Graphics.DrawArc(accentPen, arcBounds, angle, 90);
            }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                animationTimer.Stop();
                animationTimer.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}
