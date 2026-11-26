using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using Microsoft.Win32;

namespace IpoLauncher
{
    internal sealed class Config
    {
        private readonly Dictionary<string, string> map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        public static Config Load(string path)
        {
            Config c = new Config();
            foreach (string raw in File.ReadAllLines(path, Encoding.UTF8))
            {
                string line = raw.Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;
                int i = line.IndexOf('=');
                if (i <= 0) continue;
                c.map[line.Substring(0, i).Trim()] = line.Substring(i + 1).Trim();
            }
            return c;
        }

        public string Get(string key, string fallback)
        {
            string v;
            return map.TryGetValue(key, out v) && v.Length > 0 ? v : fallback;
        }

        public int GetInt(string key, int fallback)
        {
            int v;
            return int.TryParse(Get(key, ""), out v) ? v : fallback;
        }
    }

    internal static class Native
    {
        public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        [DllImport("user32.dll")]
        public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);

        [DllImport("user32.dll")]
        public static extern bool IsWindowVisible(IntPtr hWnd);

        [DllImport("user32.dll")]
        public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

        [DllImport("user32.dll")]
        public static extern bool SetForegroundWindow(IntPtr hWnd);

        [DllImport("user32.dll")]
        public static extern bool ReleaseCapture();

        [DllImport("user32.dll")]
        public static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);

        public const int WM_NCLBUTTONDOWN = 0xA1;
        public const int HTCAPTION = 2;

        // 无边框窗口自己补系统缩放：以非客户区左键按下把窗口交给系统的原生拖动/拉伸循环，
        // 拖动、Aero Snap、八方向拉伸都按系统行为走，比在 JS 里逐帧 SetWindowPos 稳。
        public static void BeginNativeDrag(IntPtr hWnd, int hit)
        {
            ReleaseCapture();
            SendMessage(hWnd, WM_NCLBUTTONDOWN, (IntPtr)hit, IntPtr.Zero);
        }

        public static int HitCode(string dir)
        {
            switch (dir)
            {
                case "l": return 10;   // HTLEFT
                case "r": return 11;   // HTRIGHT
                case "t": return 12;   // HTTOP
                case "tl": return 13;  // HTTOPLEFT
                case "tr": return 14;  // HTTOPRIGHT
                case "b": return 15;   // HTBOTTOM
                case "bl": return 16;  // HTBOTTOMLEFT
                case "br": return 17;  // HTBOTTOMRIGHT
                default: return 0;
            }
        }

        public static IntPtr FindByTitle(string title)
        {
            IntPtr found = IntPtr.Zero;
            EnumWindows(delegate(IntPtr h, IntPtr p)
            {
                if (!IsWindowVisible(h)) return true;
                StringBuilder sb = new StringBuilder(512);
                GetWindowText(h, sb, sb.Capacity);
                if (sb.ToString() == title)
                {
                    found = h;
                    return false;
                }
                return true;
            }, IntPtr.Zero);
            return found;
        }

        public static bool FocusByTitle(string title)
        {
            IntPtr h = FindByTitle(title);
            if (h == IntPtr.Zero) return false;
            ShowWindow(h, 9);
            SetForegroundWindow(h);
            return true;
        }
    }

    internal sealed class ShellForm : Form
    {
        private readonly Config cfg;
        private readonly string title;
        private readonly string statePath;
        private readonly string retryFlag;
        private readonly string failureHtml;
        private readonly string connectingHtml;
        private readonly string splashHtml;
        private readonly string logPath;
        private readonly string autostartName;
        private readonly string autostartCmd;

        private WebView2 web;
        private NotifyIcon tray;
        private ToolStripMenuItem autoItem;
        private System.Windows.Forms.Timer timer;
        private bool exiting;
        private string navKey = "";
        private string lastUrl;
        private FormWindowState lastState = FormWindowState.Normal;

        public ShellForm(Config c)
        {
            cfg = c;
            title = c.Get("title", "人生重启系统");
            statePath = c.Get("state", "");
            retryFlag = c.Get("retry_flag", "");
            logPath = c.Get("log", "");
            autostartName = c.Get("autostart_name", "");
            autostartCmd = c.Get("autostart_cmd", "");
            Text = title;
            // 无边框：系统标题栏彻底去掉，顶部改由 app/web/shell-titlebar.js 自绘。
            FormBorderStyle = FormBorderStyle.None;
            Width = c.GetInt("width", 1280);
            Height = c.GetInt("height", 820);
            MinimumSize = new Size(c.GetInt("minwidth", 980), c.GetInt("minheight", 640));
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(16, 15, 13);
            string iconPath = c.Get("icon", "");
            if (iconPath.Length > 0 && File.Exists(iconPath))
            {
                try { Icon = new Icon(iconPath); } catch { }
            }

            splashHtml = ReadOrEmpty(c.Get("splash", ""));
            failureHtml = ReadOrEmpty(c.Get("failure", ""));
            connectingHtml = ReadOrEmpty(c.Get("connecting", ""));
        }

        private static string ReadOrEmpty(string path)
        {
            try { return path.Length > 0 && File.Exists(path) ? File.ReadAllText(path, Encoding.UTF8) : ""; }
            catch { return ""; }
        }

        protected override void OnLoad(EventArgs e)
        {
            base.OnLoad(e);
            UpdateMaximizedBounds();
            BuildTray();
            web = new WebView2();
            web.Dock = DockStyle.Fill;
            Controls.Add(web);
            Shown += async delegate { await InitWebAsync(); };
        }

        /// <summary>无边框窗体自己最大化会盖住任务栏，这里把最大化范围钉在工作区。</summary>
        private void UpdateMaximizedBounds()
        {
            try { MaximizedBounds = Screen.FromHandle(Handle).WorkingArea; }
            catch { }
        }

        protected override void OnResize(EventArgs e)
        {
            base.OnResize(e);
            if (WindowState == FormWindowState.Minimized) return;
            if (WindowState == lastState) return;
            lastState = WindowState;
            NotifyWindowState();
        }

        /// <summary>把最大化/还原状态推给页面，自绘标题条据此切换图标。</summary>
        private void NotifyWindowState()
        {
            try
            {
                if (web == null || web.CoreWebView2 == null) return;
                web.CoreWebView2.PostWebMessageAsString(
                    WindowState == FormWindowState.Maximized ? "shell:state:max" : "shell:state:normal");
            }
            catch { }
        }

        private async Task InitWebAsync()
        {
            try
            {
                string userData = cfg.Get("userdata", Path.Combine(Path.GetTempPath(), "ipo-launcher-webview2"));
                Directory.CreateDirectory(userData);
                CoreWebView2Environment env = await CoreWebView2Environment.CreateAsync(null, userData, null);
                await web.EnsureCoreWebView2Async(env);

                web.CoreWebView2.Settings.AreDevToolsEnabled = false;
                web.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
                web.CoreWebView2.Settings.IsStatusBarEnabled = false;
                web.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = true;
                web.CoreWebView2.NewWindowRequested += delegate(object s, CoreWebView2NewWindowRequestedEventArgs a)
                {
                    a.Handled = true;
                    try { Process.Start(a.Uri); } catch { }
                };
                web.CoreWebView2.WebMessageReceived += OnWebMessage;
                web.CoreWebView2.NavigationCompleted += delegate { NotifyWindowState(); };

                if (splashHtml.Length > 0) web.NavigateToString(splashHtml);

                timer = new System.Windows.Forms.Timer();
                timer.Interval = 500;
                timer.Tick += delegate { PollState(); };
                timer.Start();
            }
            catch (Exception ex)
            {
                Log("webview2 init failed: " + ex);
                MessageBox.Show("窗口组件启动失败。\n\n" + ex.Message + "\n\n可改用浏览器打开，或安装 WebView2 运行时后重试。",
                    title, MessageBoxButtons.OK, MessageBoxIcon.Error);
                Environment.Exit(2);
            }
        }

        private void PollState()
        {
            if (exiting) return;
            string json;
            try
            {
                if (!File.Exists(statePath)) return;
                json = File.ReadAllText(statePath, Encoding.UTF8);
            }
            catch { return; }

            string phase = JsonValue(json, "phase");
            string message = JsonValue(json, "message");
            string url = JsonValue(json, "url");
            if (url.Length > 0) lastUrl = url;

            if (phase == "ready" && url.Length > 0)
            {
                string key = "url:" + url;
                if (navKey != key)
                {
                    navKey = key;
                    web.CoreWebView2.Navigate(url);
                }
                return;
            }

            if (phase == "failed")
            {
                if (navKey != "fail" && failureHtml.Length > 0)
                {
                    navKey = "fail";
                    web.NavigateToString(failureHtml);
                }
                return;
            }

            if (phase == "connecting")
            {
                if (navKey != "connect" && connectingHtml.Length > 0)
                {
                    navKey = "connect";
                    web.NavigateToString(connectingHtml);
                }
                return;
            }

            // starting / 其它：留在 splash，把进度推给页面
            if (navKey != "splash")
            {
                navKey = "splash";
                if (splashHtml.Length > 0) web.NavigateToString(splashHtml);
            }
            try { web.ExecuteScriptAsync("window.__setState && window.__setState(" + json + ");"); } catch { }
        }

        private static string JsonValue(string json, string key)
        {
            string needle = "\"" + key + "\"";
            int i = json.IndexOf(needle, StringComparison.Ordinal);
            if (i < 0) return "";
            i = json.IndexOf(':', i + needle.Length);
            if (i < 0) return "";
            i++;
            while (i < json.Length && char.IsWhiteSpace(json[i])) i++;
            if (i >= json.Length || json[i] != '"') return "";
            i++;
            StringBuilder sb = new StringBuilder();
            while (i < json.Length)
            {
                char ch = json[i++];
                if (ch == '\\' && i < json.Length)
                {
                    char n = json[i++];
                    if (n == 'n') sb.Append('\n');
                    else if (n == 't') sb.Append('\t');
                    else sb.Append(n);
                    continue;
                }
                if (ch == '"') break;
                sb.Append(ch);
            }
            return sb.ToString();
        }

        private void OnWebMessage(object sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            string msg;
            try { msg = e.TryGetWebMessageAsString(); }
            catch { return; }
            if (msg == null) return;
            msg = msg.Trim().Trim('"');
            if (msg.StartsWith("shell:"))
            {
                HandleShell(msg);
                return;
            }
            if (msg == "retry")
            {
                try
                {
                    if (retryFlag.Length > 0) File.WriteAllText(retryFlag, DateTime.Now.ToString("o"));
                }
                catch { }
                navKey = "";
                if (splashHtml.Length > 0) web.NavigateToString(splashHtml);
            }
            else if (msg == "openlog")
            {
                OpenLog();
            }
            else if (msg == "copylog")
            {
                try { Clipboard.SetText(logPath); } catch { }
            }
            else if (msg == "browser")
            {
                OpenBrowser();
            }
        }

        /// <summary>自绘标题条发来的窗口操作：拖动 / 最小化 / 最大化还原 / 关闭（缩托盘）/ 八向拉伸。</summary>
        private void HandleShell(string msg)
        {
            if (msg == "shell:drag")
            {
                Native.BeginNativeDrag(Handle, Native.HTCAPTION);
            }
            else if (msg == "shell:min")
            {
                WindowState = FormWindowState.Minimized;
            }
            else if (msg == "shell:max")
            {
                WindowState = WindowState == FormWindowState.Maximized
                    ? FormWindowState.Normal
                    : FormWindowState.Maximized;
            }
            else if (msg == "shell:close")
            {
                Close();   // 走 OnFormClosing：隐藏到托盘，不停后端
            }
            else if (msg.StartsWith("shell:resize:"))
            {
                int hit = Native.HitCode(msg.Substring("shell:resize:".Length));
                if (hit != 0) Native.BeginNativeDrag(Handle, hit);
            }
        }

        private void OpenLog()
        {
            try
            {
                if (logPath.Length == 0) return;
                string dir = Directory.Exists(logPath) ? logPath : Path.GetDirectoryName(logPath);
                if (dir != null && Directory.Exists(dir)) Process.Start("explorer.exe", "\"" + dir + "\"");
            }
            catch { }
        }

        private void OpenBrowser()
        {
            try
            {
                if (!string.IsNullOrEmpty(lastUrl)) Process.Start(lastUrl);
            }
            catch { }
        }

        private void BuildTray()
        {
            ContextMenuStrip menu = new ContextMenuStrip();
            ToolStripMenuItem show = new ToolStripMenuItem(cfg.Get("tray_show", "显示主界面"));
            show.Click += delegate { ShowMain(); };
            ToolStripMenuItem browser = new ToolStripMenuItem(cfg.Get("tray_browser", "用浏览器打开"));
            browser.Click += delegate { OpenBrowser(); };
            ToolStripMenuItem logItem = new ToolStripMenuItem("打开日志目录");
            logItem.Click += delegate { OpenLog(); };
            autoItem = new ToolStripMenuItem(cfg.Get("tray_autostart", "开机自启"));
            autoItem.Click += delegate { ToggleAutostart(); };
            ToolStripMenuItem quit = new ToolStripMenuItem(cfg.Get("tray_quit", "退出"));
            quit.Click += delegate { ExitApp(); };
            menu.Items.Add(show);
            menu.Items.Add(browser);
            menu.Items.Add(logItem);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(autoItem);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(quit);
            menu.Opening += delegate { if (autoItem != null) autoItem.Checked = IsAutostartOn(); };

            tray = new NotifyIcon();
            tray.Text = cfg.Get("tray_tooltip", title);
            tray.Icon = Icon != null ? Icon : SystemIcons.Application;
            tray.ContextMenuStrip = menu;
            tray.Visible = true;
            tray.DoubleClick += delegate { ShowMain(); };
        }

        private bool IsAutostartOn()
        {
            try
            {
                using (RegistryKey k = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", false))
                {
                    return k != null && k.GetValue(autostartName) != null;
                }
            }
            catch { return false; }
        }

        private void ToggleAutostart()
        {
            try
            {
                using (RegistryKey k = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
                {
                    if (k == null) return;
                    if (IsAutostartOn()) k.DeleteValue(autostartName, false);
                    else k.SetValue(autostartName, autostartCmd);
                }
            }
            catch (Exception ex) { Log("autostart toggle failed: " + ex.Message); }
        }

        private void ShowMain()
        {
            Show();
            if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
            Activate();
            TopMost = true;
            TopMost = false;
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (!exiting)
            {
                e.Cancel = true;
                Hide();
                try { tray.ShowBalloonTip(2000, title, "已最小化到托盘，双击图标可重新打开。", ToolTipIcon.Info); }
                catch { }
                return;
            }
            base.OnFormClosing(e);
        }

        private void ExitApp()
        {
            exiting = true;
            try { if (timer != null) timer.Stop(); } catch { }
            try { if (tray != null) { tray.Visible = false; tray.Dispose(); } } catch { }
            try { if (web != null) web.Dispose(); } catch { }
            Close();
            Application.Exit();
        }

        private static void Log(string msg)
        {
            try
            {
                string dir = Path.Combine(Path.GetTempPath(), "ipo-launcher");
                Directory.CreateDirectory(dir);
                File.AppendAllText(Path.Combine(dir, "host.log"), DateTime.Now.ToString("s") + " " + msg + Environment.NewLine, Encoding.UTF8);
            }
            catch { }
        }

        // 供 FormClosing 之前保存上次状态使用（保留扩展点）
        public string LastUrl { get { return lastUrl; } }
    }

    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            if (args.Length < 1)
            {
                MessageBox.Show("缺少启动配置文件。", "启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            Config cfg = Config.Load(args[0]);
            string mutexName = cfg.Get("mutex", "ipo_launcher_host");
            bool created;
            Mutex mutex = new Mutex(true, mutexName, out created);
            if (!created)
            {
                if (!Native.FocusByTitle(cfg.Get("title", "人生重启系统")))
                {
                    MessageBox.Show("程序已经在运行（可能最小化到了托盘，请查看任务栏右下角）。",
                        cfg.Get("title", "人生重启系统"), MessageBoxButtons.OK, MessageBoxIcon.Information);
                }
                return;
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new ShellForm(cfg));
            GC.KeepAlive(mutex);
        }
    }
}
