using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace PortaMCPLauncher
{
    internal sealed class PythonCommand
    {
        public readonly string Executable;
        public readonly string PrefixArguments;

        public PythonCommand(string executable, string prefixArguments)
        {
            Executable = executable;
            PrefixArguments = prefixArguments ?? "";
        }
    }

    internal static class Program
    {
        private const string BootstrapArchiveRelativePath = "runtime\\python-bootstrap.zip";
        private const string BootstrapHashRelativePath = "runtime\\python-bootstrap.sha256";

        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string root = AppDomain.CurrentDomain.BaseDirectory
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);

            if (!File.Exists(Path.Combine(root, "porta_mcp", "installer.py")))
            {
                ShowError("The PortaMCP application files are incomplete. Extract the complete release bundle and try again.");
                return;
            }

            if (EnvironmentReady(root))
            {
                if (!LaunchControlCenter(root))
                    ShowError("The PortaMCP environment is ready, but the Control Center could not be launched.");
                return;
            }

            Application.Run(new BootstrapForm(root));
        }

        private static string Quote(string value)
        {
            return "\"" + value.Replace("\"", "\\\"") + "\"";
        }

        private static void ShowError(string text)
        {
            MessageBox.Show(text, "PortaMCP", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }

        private static bool RunCheck(PythonCommand command, string root, string code, int timeoutMs)
        {
            if (Path.IsPathRooted(command.Executable) && !File.Exists(command.Executable))
                return false;
            try
            {
                using (Process process = Process.Start(new ProcessStartInfo
                {
                    FileName = command.Executable,
                    Arguments = command.PrefixArguments + "-c " + Quote(code),
                    WorkingDirectory = root,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden
                }))
                {
                    if (process == null)
                        return false;
                    if (!process.WaitForExit(timeoutMs))
                    {
                        try { process.Kill(); } catch { }
                        return false;
                    }
                    return process.ExitCode == 0;
                }
            }
            catch (Win32Exception) { return false; }
            catch (FileNotFoundException) { return false; }
            catch { return false; }
        }

        private static bool IsBootstrapPython(PythonCommand command, string root)
        {
            const string check = "import sys, tkinter, venv, ensurepip; raise SystemExit(0 if sys.version_info >= (3, 11) else 9)";
            return RunCheck(command, root, check, 10000);
        }

        private static PythonCommand FindBootstrapPython(string root)
        {
            // The Windows release is self-contained: first-run setup never depends
            // on PATH, the Python Launcher, or a machine-wide Python installation.
            // Reuse only the private runtime that PortaMCP previously extracted.
            string privateRoot = Path.Combine(root, ".portamcp", "bootstrap-python");
            PythonCommand[] candidates = new PythonCommand[]
            {
                new PythonCommand(Path.Combine(privateRoot, "python.exe"), ""),
                new PythonCommand(Path.Combine(privateRoot, "pythonw.exe"), "")
            };

            foreach (PythonCommand candidate in candidates)
            {
                if (IsBootstrapPython(candidate, root))
                    return candidate;
            }
            return null;
        }

        private static bool EnvironmentReady(string root)
        {
            string python = Path.Combine(root, ".venv", "Scripts", "python.exe");
            if (!File.Exists(python))
                return false;
            const string check =
                "from pathlib import Path; import mcp, customtkinter, playwright, websockets, PIL, porta_mcp; " +
                "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); " +
                "ok=Path(p.chromium.executable_path).is_file(); p.stop(); raise SystemExit(0 if ok else 1)";
            return RunCheck(new PythonCommand(python, ""), root, check, 20000);
        }

        private static bool LaunchControlCenter(string root)
        {
            string pythonw = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
            string python = Path.Combine(root, ".venv", "Scripts", "python.exe");
            string executable = File.Exists(pythonw) ? pythonw : python;
            if (!File.Exists(executable))
                return false;
            try
            {
                Process.Start(new ProcessStartInfo
                {
                    FileName = executable,
                    Arguments = "-m porta_mcp.control_center",
                    WorkingDirectory = root,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden
                });
                return true;
            }
            catch (Win32Exception) { return false; }
            catch (FileNotFoundException) { return false; }
        }

        private static int RunInstaller(PythonCommand command, string root)
        {
            string runtimeDir = Path.Combine(root, ".portamcp");
            Directory.CreateDirectory(runtimeDir);
            string setupLog = Path.Combine(runtimeDir, "setup.log");
            try { File.WriteAllText(setupLog, "PortaMCP first-run setup\r\n", Encoding.UTF8); } catch { }

            ProcessStartInfo info = new ProcessStartInfo
            {
                FileName = command.Executable,
                Arguments = command.PrefixArguments + "-u -m porta_mcp.installer",
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };

            using (Process process = Process.Start(info))
            {
                if (process == null)
                    throw new InvalidOperationException("The PortaMCP installer could not be started.");

                using (StreamWriter log = new StreamWriter(setupLog, true, Encoding.UTF8))
                {
                    object sync = new object();
                    process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs args)
                    {
                        if (args.Data == null) return;
                        lock (sync) { log.WriteLine(args.Data); log.Flush(); }
                    };
                    process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs args)
                    {
                        if (args.Data == null) return;
                        lock (sync) { log.WriteLine(args.Data); log.Flush(); }
                    };
                    process.BeginOutputReadLine();
                    process.BeginErrorReadLine();
                    process.WaitForExit();
                    process.WaitForExit();
                    return process.ExitCode;
                }
            }
        }

        private static string Sha256(string path)
        {
            using (SHA256 sha = SHA256.Create())
            using (FileStream stream = File.OpenRead(path))
            {
                byte[] hash = sha.ComputeHash(stream);
                StringBuilder builder = new StringBuilder(hash.Length * 2);
                foreach (byte value in hash)
                    builder.Append(value.ToString("x2"));
                return builder.ToString();
            }
        }

        private static bool IsSha256Text(string value)
        {
            if (value == null || value.Length != 64)
                return false;
            for (int i = 0; i < value.Length; ++i)
            {
                char c = value[i];
                bool hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
                if (!hex)
                    return false;
            }
            return true;
        }

        private static string ReadExpectedBootstrapHash(string root)
        {
            string path = Path.Combine(root, BootstrapHashRelativePath);
            if (!File.Exists(path))
                throw new FileNotFoundException("The bundled Python runtime checksum is missing. Extract the complete Windows release bundle and try again.", path);
            string value = File.ReadAllText(path, Encoding.ASCII).Trim();
            if (!IsSha256Text(value))
                throw new InvalidDataException("The bundled Python runtime checksum is invalid. Setup stopped safely.");
            return value.ToLowerInvariant();
        }

        private static void ExtractBootstrapArchive(string archive, string destination)
        {
            string destinationRoot = Path.GetFullPath(destination).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            Directory.CreateDirectory(destinationRoot);
            using (ZipArchive zip = ZipFile.OpenRead(archive))
            {
                foreach (ZipArchiveEntry entry in zip.Entries)
                {
                    string relative = entry.FullName.Replace('/', Path.DirectorySeparatorChar);
                    if (string.IsNullOrWhiteSpace(relative))
                        continue;
                    string target = Path.GetFullPath(Path.Combine(destinationRoot, relative));
                    if (!target.StartsWith(destinationRoot, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidDataException("The bundled Python runtime archive contains an unsafe path. Setup stopped safely.");

                    if (string.IsNullOrEmpty(entry.Name))
                    {
                        Directory.CreateDirectory(target);
                        continue;
                    }

                    string parent = Path.GetDirectoryName(target);
                    if (!string.IsNullOrEmpty(parent))
                        Directory.CreateDirectory(parent);
                    entry.ExtractToFile(target, true);
                }
            }
        }

        private static void PromoteDirectoryWithRetry(string source, string destination)
        {
            Exception lastError = null;
            const int attempts = 20;
            for (int attempt = 1; attempt <= attempts; ++attempt)
            {
                try
                {
                    Directory.Move(source, destination);
                    return;
                }
                catch (UnauthorizedAccessException exc)
                {
                    lastError = exc;
                }
                catch (IOException exc)
                {
                    lastError = exc;
                }

                // A freshly executed portable runtime can remain briefly held by
                // antivirus/indexing software even after the validation process
                // exits. Keep setup fail-closed, but tolerate that short-lived
                // Windows lock instead of requiring a manual Retry.
                if (attempt < attempts)
                    Thread.Sleep(200);
            }

            throw new IOException(
                "The private Python runtime could not be finalized after repeated attempts. Close software scanning the PortaMCP folder and retry.",
                lastError
            );
        }

        private static PythonCommand BootstrapPython(string root, Action<string> status)
        {
            PythonCommand existing = FindBootstrapPython(root);
            if (existing != null)
                return existing;

            string archive = Path.Combine(root, BootstrapArchiveRelativePath);
            if (!File.Exists(archive))
                throw new FileNotFoundException(
                    "No compatible Python installation was found and the bundled Python runtime is missing. Extract the complete Windows release bundle and try again.",
                    archive
                );

            status("Verifying the bundled Python runtime...");
            string expectedHash = ReadExpectedBootstrapHash(root);
            string actualHash = Sha256(archive);
            if (!string.Equals(actualHash, expectedHash, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("The bundled Python runtime failed SHA-256 verification. Setup stopped safely.");

            string runtimeParent = Path.Combine(root, ".portamcp");
            string runtimeDir = Path.Combine(runtimeParent, "bootstrap-python");
            string stagingDir = Path.Combine(runtimeParent, "bootstrap-python.tmp-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(runtimeParent);

            status("Preparing the private PortaMCP Python runtime...");
            try
            {
                if (Directory.Exists(stagingDir))
                    Directory.Delete(stagingDir, true);
                ExtractBootstrapArchive(archive, stagingDir);

                PythonCommand staged = new PythonCommand(Path.Combine(stagingDir, "python.exe"), "");
                if (!IsBootstrapPython(staged, root))
                    throw new InvalidDataException("The bundled Python runtime was extracted but did not pass validation.");

                if (Directory.Exists(runtimeDir))
                    Directory.Delete(runtimeDir, true);
                PromoteDirectoryWithRetry(stagingDir, runtimeDir);

                PythonCommand installed = new PythonCommand(Path.Combine(runtimeDir, "python.exe"), "");
                if (!IsBootstrapPython(installed, root))
                    throw new InvalidDataException("The private Python runtime did not pass final validation.");
                return installed;
            }
            finally
            {
                try
                {
                    if (Directory.Exists(stagingDir))
                        Directory.Delete(stagingDir, true);
                }
                catch { }
            }
        }

        private sealed class BootstrapForm : Form
        {
            private readonly string root;
            private readonly Label status;
            private readonly Label detail;
            private readonly ProgressBar progress;
            private readonly Button retry;
            private bool running;

            public BootstrapForm(string rootPath)
            {
                root = rootPath;
                Text = "PortaMCP Setup";
                Width = 590;
                Height = 270;
                FormBorderStyle = FormBorderStyle.FixedDialog;
                MaximizeBox = false;
                MinimizeBox = true;
                StartPosition = FormStartPosition.CenterScreen;
                BackColor = System.Drawing.Color.FromArgb(11, 15, 22);
                ForeColor = System.Drawing.Color.White;

                Label title = new Label();
                title.Text = "PortaMCP";
                title.Font = new System.Drawing.Font("Segoe UI", 20, System.Drawing.FontStyle.Bold);
                title.AutoSize = true;
                title.Left = 28;
                title.Top = 24;
                Controls.Add(title);

                detail = new Label();
                detail.Text = "First launch is automatic. PortaMCP prepares Python if needed, creates its private virtual environment, installs dependencies and the managed browser, then opens the Control Center.";
                detail.Font = new System.Drawing.Font("Segoe UI", 9);
                detail.ForeColor = System.Drawing.Color.FromArgb(160, 176, 196);
                detail.Left = 30;
                detail.Top = 66;
                detail.Width = 520;
                detail.Height = 54;
                Controls.Add(detail);

                status = new Label();
                status.Text = "Preparing setup...";
                status.Font = new System.Drawing.Font("Segoe UI", 10, System.Drawing.FontStyle.Bold);
                status.Left = 30;
                status.Top = 128;
                status.Width = 520;
                status.Height = 22;
                Controls.Add(status);

                progress = new ProgressBar();
                progress.Style = ProgressBarStyle.Marquee;
                progress.MarqueeAnimationSpeed = 28;
                progress.Left = 30;
                progress.Top = 158;
                progress.Width = 520;
                progress.Height = 12;
                Controls.Add(progress);

                retry = new Button();
                retry.Text = "Retry";
                retry.Visible = false;
                retry.Width = 90;
                retry.Height = 30;
                retry.Left = 460;
                retry.Top = 186;
                retry.Click += delegate { StartBootstrap(); };
                Controls.Add(retry);

                Shown += delegate { StartBootstrap(); };
            }

            private void SetStatus(string text)
            {
                if (IsDisposed)
                    return;
                BeginInvoke((MethodInvoker)delegate { status.Text = text; });
            }

            private void StartBootstrap()
            {
                if (running)
                    return;
                running = true;
                retry.Visible = false;
                progress.Visible = true;
                status.ForeColor = System.Drawing.Color.White;
                status.Text = "Checking local prerequisites...";

                Thread worker = new Thread(delegate()
                {
                    try
                    {
                        PythonCommand runtime = Program.BootstrapPython(root, SetStatus);
                        SetStatus("Creating the virtual environment and installing PortaMCP...");
                        int code = Program.RunInstaller(runtime, root);
                        if (code != 0)
                            throw new InvalidOperationException("PortaMCP dependency installation exited with code " + code + ". Details are in .portamcp\\setup.log.");
                        SetStatus("Validating the completed installation...");
                        if (!Program.EnvironmentReady(root))
                            throw new InvalidOperationException("Setup completed, but the private PortaMCP environment did not pass validation.");
                        SetStatus("Setup complete. Opening PortaMCP...");
                        if (!Program.LaunchControlCenter(root))
                            throw new InvalidOperationException("The Control Center could not be launched after setup.");
                        BeginInvoke((MethodInvoker)delegate { Close(); });
                    }
                    catch (Exception exc)
                    {
                        BeginInvoke((MethodInvoker)delegate
                        {
                            running = false;
                            progress.Visible = false;
                            retry.Visible = true;
                            status.ForeColor = System.Drawing.Color.FromArgb(255, 182, 186);
                            status.Text = "Setup needs attention";
                            detail.Text = exc.Message + "\r\nResolve the issue above and choose Retry. Setup details are written under .portamcp when available.";
                        });
                    }
                });
                worker.IsBackground = true;
                worker.Start();
            }
        }
    }
}
