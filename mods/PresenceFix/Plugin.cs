using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Threading;
using HarmonyLib;
using IPA;
using IPA.Utilities;
using IPALogger = IPA.Logging.Logger;

namespace PresenceFix
{
    // Since 1.45, PauseController treats OpenXR XR_EXT_user_presence as a pause condition.
    // When the Index proximity sensor never reaches its firmware threshold (~448), songs pause on the
    // first frame and re-pause after every resume. This plugin takes over UnityXRSystemState.SetUserPresence
    // and supplies the presence value according to UserData/PresenceFix.ini:
    //   AlwaysWorn - always true
    //   Threshold  - read the raw proximity count from lighthouse_console and apply a software
    //                threshold (hysteresis + leave debounce)
    [Plugin(RuntimeOptions.SingleStartInit)]
    public class Plugin
    {
        internal static IPALogger Log;
        private Harmony _harmony;

        [Init]
        public Plugin(IPALogger logger)
        {
            Log = logger;
            Settings.Load();
            _harmony = new Harmony("com.local.presencefix");
            _harmony.PatchAll(Assembly.GetExecutingAssembly());
            Log.Info($"Took over UnityXRSystemState.SetUserPresence, mode {Settings.Mode}");
        }

        [OnExit]
        public void OnExit()
        {
            _harmony?.UnpatchSelf();
            RawProximity.Stop();
        }
    }

    internal enum PresenceMode
    {
        AlwaysWorn,
        Threshold
    }

    internal static class Settings
    {
        public static PresenceMode Mode = PresenceMode.AlwaysWorn;
        public static int OnThreshold = 260;
        public static int OffThreshold = 220;
        public static float OffDelaySeconds = 1f;
        public static string Serial = "";

        private static readonly string Path = System.IO.Path.Combine(UnityGame.UserDataPath, "PresenceFix.ini");
        private static DateTime _loadedWriteTime;
        private static float _nextCheck;

        private const string DefaultContent =
            "; Mode = AlwaysWorn | Threshold\n" +
            "; Threshold reads the headset's raw proximity count: >= OnThreshold means worn,\n" +
            "; <= OffThreshold for OffDelaySeconds means not worn.\n" +
            "; Threshold 模式读取头显接近传感器原始计数：>= OnThreshold 判为佩戴，<= OffThreshold 持续 OffDelaySeconds 秒判为离开。\n" +
            "; Tune with tools/proximity_overlay.py; changes apply about 2 s after saving.\n" +
            "; 用 tools/proximity_overlay.py 对照读数调整；保存后约 2 秒生效。\n" +
            "Mode = AlwaysWorn\n" +
            "OnThreshold = 260\n" +
            "OffThreshold = 220\n" +
            "OffDelaySeconds = 1.0\n" +
            "; Empty = first LHR- receiver (usually the HMD) / 留空自动选第一个 LHR- 接收器（通常是头显）\n" +
            "Serial =\n";

        public static void Load()
        {
            try
            {
                if (!File.Exists(Path))
                {
                    File.WriteAllText(Path, DefaultContent);
                }
                _loadedWriteTime = File.GetLastWriteTimeUtc(Path);
                foreach (string rawLine in File.ReadAllLines(Path))
                {
                    string line = rawLine.Trim();
                    if (line.Length == 0 || line.StartsWith(";") || line.StartsWith("#"))
                    {
                        continue;
                    }
                    int eq = line.IndexOf('=');
                    if (eq < 0)
                    {
                        continue;
                    }
                    string key = line.Substring(0, eq).Trim();
                    string value = line.Substring(eq + 1).Trim();
                    switch (key)
                    {
                        case "Mode":
                            Mode = string.Equals(value, "Threshold", StringComparison.OrdinalIgnoreCase)
                                ? PresenceMode.Threshold : PresenceMode.AlwaysWorn;
                            break;
                        case "OnThreshold":
                            int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out OnThreshold);
                            break;
                        case "OffThreshold":
                            int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out OffThreshold);
                            break;
                        case "OffDelaySeconds":
                            float.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out OffDelaySeconds);
                            break;
                        case "Serial":
                            Serial = value;
                            break;
                    }
                }
                if (OffThreshold >= OnThreshold)
                {
                    OffThreshold = OnThreshold - 1;
                }
                Plugin.Log?.Info($"Config: Mode={Mode} On={OnThreshold} Off={OffThreshold} OffDelay={OffDelaySeconds}s Serial={(Serial.Length > 0 ? Serial : "auto")}");
            }
            catch (Exception e)
            {
                Mode = PresenceMode.AlwaysWorn;
                Plugin.Log?.Warn($"Failed to read {Path}, falling back to AlwaysWorn: {e.Message}");
            }
        }

        // Called every frame on the main thread; checks the file timestamp every 2 s and reloads on change
        public static void PollReload(float now)
        {
            if (now < _nextCheck)
            {
                return;
            }
            _nextCheck = now + 2f;
            try
            {
                if (File.GetLastWriteTimeUtc(Path) != _loadedWriteTime)
                {
                    PresenceMode before = Mode;
                    Load();
                    if (before == PresenceMode.Threshold && Mode != PresenceMode.Threshold)
                    {
                        RawProximity.Stop();
                    }
                }
            }
            catch (IOException)
            {
            }
        }
    }

    internal static class RawProximity
    {
        private static readonly Regex ProxLine = new Regex(@"proximity=(\d+) \([\d.]+\) prox_triggered=(\d)", RegexOptions.Compiled);
        private static readonly Regex ReceiverLine = new Regex(@"^\s+(LHR-[0-9A-Fa-f]+)\s*$", RegexOptions.Compiled);

        private static Process _proc;
        private static int _value = -1;
        private static long _lastSampleTicks;
        private static float _retryAt;

        public static int Value => _value;

        // No new sample for 3 s counts as a data gap
        public static bool IsFresh => _value >= 0 && DateTime.UtcNow.Ticks - Interlocked.Read(ref _lastSampleTicks) < TimeSpan.TicksPerSecond * 3;

        public static void EnsureRunning(float now)
        {
            if (_proc != null && !_proc.HasExited)
            {
                return;
            }
            if (now < _retryAt)
            {
                return;
            }
            _retryAt = now + 10f;
            Start();
        }

        private static void Start()
        {
            string exe = FindLighthouseConsole();
            if (exe == null)
            {
                Plugin.Log.Warn("lighthouse_console.exe not found; Threshold mode treats the headset as worn");
                return;
            }
            try
            {
                var psi = new ProcessStartInfo(exe)
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardInput = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };
                _proc = Process.Start(psi);
                ChildJob.Attach(_proc);
                var reader = new Thread(ReadLoop) { IsBackground = true, Name = "PresenceFix.lighthouse_console" };
                reader.Start(_proc);
                Plugin.Log.Info($"Started {exe}");
            }
            catch (Exception e)
            {
                _proc = null;
                Plugin.Log.Warn($"Failed to start lighthouse_console: {e.Message}");
            }
        }

        private static void ReadLoop(object state)
        {
            var proc = (Process)state;
            bool commandsSent = false;
            string firstReceiver = null;
            try
            {
                string line;
                while ((line = proc.StandardOutput.ReadLine()) != null)
                {
                    if (!commandsSent)
                    {
                        Match receiver = ReceiverLine.Match(line);
                        if (receiver.Success && firstReceiver == null)
                        {
                            firstReceiver = receiver.Groups[1].Value;
                        }
                        // Send commands once the device list ends with "Connected to receiver" or the prompt
                        if (line.Contains("Connected to receiver") || line.StartsWith("lh>"))
                        {
                            string serial = Settings.Serial.Length > 0 ? Settings.Serial : firstReceiver;
                            // The count is in the axis stream; the console only keeps printing during sleep
                            string commands = (serial != null ? $"serial {serial}\n" : "") + "axis\ndump\nsleep 86400000\n";
                            proc.StandardInput.Write(commands);
                            proc.StandardInput.Flush();
                            commandsSent = true;
                            Plugin.Log.Info($"Reading proximity from {serial ?? "default receiver"}");
                        }
                        continue;
                    }
                    Match m = ProxLine.Match(line);
                    if (m.Success)
                    {
                        _value = int.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture);
                        Interlocked.Exchange(ref _lastSampleTicks, DateTime.UtcNow.Ticks);
                    }
                }
            }
            catch (Exception e)
            {
                Plugin.Log.Warn($"Error reading lighthouse_console output: {e.Message}");
            }
        }

        public static void Stop()
        {
            try
            {
                if (_proc != null && !_proc.HasExited)
                {
                    _proc.Kill();
                }
            }
            catch (Exception)
            {
            }
            _proc = null;
            _value = -1;
        }

        private static string FindLighthouseConsole()
        {
            const string relative = @"tools\lighthouse\bin\win64\lighthouse_console.exe";
            try
            {
                string vrpath = System.IO.Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"openvr\openvrpaths.vrpath");
                if (File.Exists(vrpath))
                {
                    Match runtime = Regex.Match(File.ReadAllText(vrpath), "\"runtime\"\\s*:\\s*\\[\\s*\"([^\"]+)\"");
                    if (runtime.Success)
                    {
                        string candidate = System.IO.Path.Combine(Regex.Unescape(runtime.Groups[1].Value), relative);
                        if (File.Exists(candidate))
                        {
                            return candidate;
                        }
                    }
                }
            }
            catch (Exception)
            {
            }
            string fallback = System.IO.Path.Combine(@"C:\Program Files (x86)\Steam\steamapps\common\SteamVR", relative);
            return File.Exists(fallback) ? fallback : null;
        }
    }

    // lighthouse_console does not exit during sleep even after stdin closes; put it in a
    // KILL_ON_JOB_CLOSE job so it dies when the game exits or crashes.
    internal static class ChildJob
    {
        private const int JobObjectExtendedLimitInformation = 9;
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
            public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

        [DllImport("kernel32.dll")]
        private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info, int length);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        private static IntPtr _job;

        public static void Attach(Process process)
        {
            try
            {
                if (_job == IntPtr.Zero)
                {
                    _job = CreateJobObject(IntPtr.Zero, null);
                    var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
                    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                    SetInformationJobObject(_job, JobObjectExtendedLimitInformation, ref info, Marshal.SizeOf(info));
                }
                if (!AssignProcessToJobObject(_job, process.Handle))
                {
                    Plugin.Log.Warn($"Failed to add lighthouse_console to job ({Marshal.GetLastWin32Error()}); it may linger if the game crashes");
                }
            }
            catch (Exception e)
            {
                Plugin.Log.Warn($"Failed to create job: {e.Message}");
            }
        }
    }

    internal static class PresenceJudge
    {
        private static bool _worn = true;
        private static float _belowOffSince = -1f;
        private static bool _warnedStale;

        public static bool Evaluate(float now)
        {
            RawProximity.EnsureRunning(now);
            if (!RawProximity.IsFresh)
            {
                if (!_warnedStale)
                {
                    _warnedStale = true;
                    Plugin.Log.Warn("No proximity data yet, treating the headset as worn");
                }
                _worn = true;
                _belowOffSince = -1f;
                return true;
            }
            _warnedStale = false;

            int value = RawProximity.Value;
            if (value >= Settings.OnThreshold)
            {
                _worn = true;
                _belowOffSince = -1f;
            }
            else if (value <= Settings.OffThreshold)
            {
                if (_belowOffSince < 0f)
                {
                    _belowOffSince = now;
                }
                if (now - _belowOffSince >= Settings.OffDelaySeconds)
                {
                    _worn = false;
                }
            }
            else
            {
                _belowOffSince = -1f;
            }
            return _worn;
        }
    }

    [HarmonyPatch(typeof(UnityXRSystemState), "SetUserPresence")]
    internal static class SetUserPresencePatch
    {
        private static bool? _lastRuntimeValue;
        private static bool? _lastResult;

        private static void Prefix(ref bool newUserPresence)
        {
            float now = UnityEngine.Time.realtimeSinceStartup;
            Settings.PollReload(now);

            if (_lastRuntimeValue != newUserPresence)
            {
                _lastRuntimeValue = newUserPresence;
                Plugin.Log.Debug($"Runtime isUserPresent={newUserPresence}");
            }

            bool result = Settings.Mode == PresenceMode.Threshold ? PresenceJudge.Evaluate(now) : true;
            if (_lastResult != result)
            {
                _lastResult = result;
                Plugin.Log.Info(Settings.Mode == PresenceMode.Threshold
                    ? $"Software decision: {(result ? "worn" : "not worn")} (raw count {RawProximity.Value})"
                    : "Presence fixed to worn");
            }
            newUserPresence = result;
        }
    }
}
