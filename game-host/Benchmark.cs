using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using HarmonyLib;
using BepInEx;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Poly.Physics;
using UnityEngine;

[BepInPlugin("local.polybridge2.benchmark", "Poly Bridge 2 Benchmark Host", "0.1.0")]
public sealed class Benchmark : BaseUnityPlugin
{
    private static string inbox;
    private static SimulationFeedback feedback;
    private string activeId;
    private string activeCase;
    private int cost;
    private float deadline;
    private float nextPoll;
    private bool ready;
    private string watch;
    private bool holding;
    private int width;
    private int height;
    private float nextResize;
    private int matchingFrames;
    private const int FastSpeedIndex = 4;
    private const float FastWallSeconds = 22;
    private const string WatchTitle = "Poly Bridge 2 Benchmark";
    private static float WatchWallSeconds
    {
        get { return FastWallSeconds * Bridge.m_SimulationSpeeds[FastSpeedIndex] / Bridge.m_SimulationSpeeds[Bridge.DEFAULT_SIMULATION_SPEED_INDEX]; }
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetWindowText(IntPtr window, string title);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr window);

    private static readonly Dictionary<string, BridgeMaterialType> Materials =
        new Dictionary<string, BridgeMaterialType> {
            { "road", BridgeMaterialType.ROAD },
            { "wood", BridgeMaterialType.WOOD },
            { "steel", BridgeMaterialType.STEEL }
        };

    private void Awake()
    {
        string root = Environment.GetEnvironmentVariable("PB2_BENCH_SESSION");
        if (String.IsNullOrEmpty(root) || !Path.IsPathRooted(root))
        {
            enabled = false;
            return;
        }
        inbox = root;
        watch = Environment.GetEnvironmentVariable("PB2_BENCH_WATCH");
        string[] size = (Environment.GetEnvironmentVariable("PB2_BENCH_SIZE") ?? "960x540").Split('x');
        width = Int32.Parse(size[0], CultureInfo.InvariantCulture);
        height = Int32.Parse(size[1], CultureInfo.InvariantCulture);
        Application.runInBackground = true;
        var harmony = new Harmony("local.polybridge2.benchmark");
        harmony.Patch(AccessTools.Method(typeof(GamePersistentPath), "SetPersistentDataDirectory"),
            prefix: new HarmonyMethod(typeof(Benchmark), "SetProfilePath"));
        harmony.Patch(AccessTools.Method(typeof(Profile), "Load"),
            postfix: new HarmonyMethod(typeof(Benchmark), "UseBenchmarkProfile"));
        harmony.Patch(AccessTools.Method(typeof(PolyPhysics.World), "FixedUpdate_Manual"),
            postfix: new HarmonyMethod(typeof(Benchmark), "SamplePhysics"));
        harmony.Patch(AccessTools.Method(typeof(PolyPhysics.BridgeEdgeListener), "OnEdgeBroken"),
            prefix: new HarmonyMethod(typeof(Benchmark), "CaptureBreak"));
    }

    private static bool SetProfilePath()
    {
        string profilePath = Path.Combine(inbox, "profile");
        Directory.CreateDirectory(Path.Combine(profilePath, "Profiles"));
        AccessTools.Field(typeof(GamePersistentPath), "m_PersistentDataDirectory").SetValue(null, profilePath);
        AccessTools.Field(typeof(GamePersistentPath), "m_CensoredPersistentDataDirectory").SetValue(null, "benchmark/profile");
        return false;
    }

    private static void UseBenchmarkProfile()
    {
        Profile.m_PlayedTutorial = true;
        Profile.m_Replays = false;
    }

    private void Update()
    {
        if (!ready && GameStateManager.GetState() != GameState.MAIN_MENU)
            return;
        // Profile.Load applies the game's resolution before the main menu. Unity
        // applies SetResolution at the end of the frame, so wait for confirmation.
        if (Screen.width != width || Screen.height != height || Screen.fullScreenMode != FullScreenMode.Windowed)
        {
            matchingFrames = 0;
            if (Time.realtimeSinceStartup >= nextResize)
            {
                Screen.SetResolution(width, height, FullScreenMode.Windowed);
                nextResize = Time.realtimeSinceStartup + 0.25f;
            }
            if (!ready)
                return;
        }
        if (!ready)
        {
            if (++matchingFrames < 2)
                return;
            if (GamePersistentPath.GetPersistentDataDirectory() != Path.Combine(inbox, "profile"))
            {
                Logger.LogError("Benchmark profile isolation failed");
                enabled = false;
                return;
            }
            AudioListener.volume = watch == "sound" ? 1 : 0;
            ready = true;
            int pid = System.Diagnostics.Process.GetCurrentProcess().Id;
            var display = new { width = Screen.width, height = Screen.height, mode = Screen.fullScreenMode.ToString() };
            if (watch != null)
            {
                using (var process = System.Diagnostics.Process.GetCurrentProcess())
                {
                    IntPtr window = process.MainWindowHandle;
                    if (window == IntPtr.Zero || !IsWindowVisible(window) || !SetWindowText(window, WatchTitle))
                    {
                        WriteJson("ready.json", new { pid, error = "Could not find and title the visible game window" });
                        return;
                    }
                }
                WriteJson("ready.json", new { protocol = 1, game_version = Application.version,
                    physics_version = GameManager.GetPhysicsEngineVersion(), pid, display,
                    watch = new { title = WatchTitle, width = Screen.width, height = Screen.height,
                        audio_volume = AudioListener.volume, simulation_wall_limit = WatchWallSeconds } });
            }
            else
                WriteJson("ready.json", new { protocol = 1, game_version = Application.version,
                    physics_version = GameManager.GetPhysicsEngineVersion(), pid, display });
        }
        if (activeId != null)
        {
            if (GameStateSim.m_LevelPassed) Finish("passed");
            else if (GameStateSim.m_LevelFailed) Finish("failed");
            else if (GameStateSim.m_LevelHung) Finish("stalled");
            else if (Time.realtimeSinceStartup >= deadline) Finish("timeout");
            return;
        }
        if (Time.realtimeSinceStartup < nextPoll)
            return;
        nextPoll = Time.realtimeSinceStartup + 0.15f;
        string file = Directory.GetFiles(inbox, "*.request.json").OrderBy(p => p).FirstOrDefault();
        if (file != null)
            Handle(file);
    }

    private void Handle(string file)
    {
        string id = Path.GetFileName(file).Split('.')[0];
        string claimed = Path.Combine(inbox, id + ".started.json");
        if (File.Exists(claimed))
            return;
        File.Move(file, claimed);
        try
        {
            var request = JObject.Parse(File.ReadAllText(claimed));
            if ((string)request["action"] == "quit")
            {
                WriteJson(id + ".result.json", new { status = "closed" });
                GameManager.QuitWithoutConfirmation();
                return;
            }
            if ((string)request["action"] == "release")
            {
                if (holding)
                {
                    GameStateManager.SwitchToStateImmediate(GameState.SANDBOX);
                    PointsOfView.SnapTo(PointOfViewType.BUILD_CUSTOM);
                    holding = false;
                }
                WriteJson(id + ".result.json", new { status = "ready" });
                return;
            }
            string caseId = (string)request["case"];
            int span;
            int budget;
            switch (caseId)
            {
                case "gap4": span = 4; budget = 6500; break;
                case "gap8": span = 8; budget = 15000; break;
                default: throw new ArgumentException("Unknown benchmark case");
            }
            var anchors = LoadScenario(span, budget, caseId);
            if ((string)request["action"] == "observe")
            {
                WriteJson(id + ".result.json", new {
                    status = "ready", @case = caseId, budget,
                    anchors = anchors.ToDictionary(p => p.Key, p => XY(p.Value.transform.position)),
                    bounds = new { xmin = 0, xmax = span, ymin = 0, ymax = 10 },
                    materials = Materials.ToDictionary(p => p.Key, p => new {
                        max_length = BridgeMaterials.GetMaxEdgeLength(p.Value),
                        cost_per_meter = BridgeMaterials.GetMaterial(p.Value).m_PricePerMeter }),
                    vehicle = "one compact car, left to right",
                    start = XY(Vehicles.m_Vehicles[0].transform.position),
                    goal = XY(VehicleStopTriggers.FindTriggerThatStopsVehicle("benchmark-car").transform.position),
                    road_y = anchors["L"].transform.position.y
                });
                return;
            }
            string action = (string)request["action"];
            if (action != "simulate" && action != "validate")
                throw new ArgumentException("Unknown action");
            var members = ApplyDesign((JObject)request["design"], anchors, span);
            cost = Mathf.RoundToInt(Budget.CalculateBridgeCost());
            if (cost > budget)
                throw new DesignException("Cost " + cost + " exceeds budget " + budget);
            if (action == "validate")
            {
                if (watch == null)
                    LoadScenario(span, budget, caseId);
                WriteJson(id + ".result.json", new { status = "valid", cost, budget });
                return;
            }
            feedback = new SimulationFeedback(members, anchors);
            Bridge.m_SimulationSpeedIndex = watch == null ? FastSpeedIndex : Bridge.DEFAULT_SIMULATION_SPEED_INDEX;
            if (watch != null)
            {
                if (!Profile.m_StressViewEnabled)
                    GameUI.m_Instance.m_TopBar.OnStressSilent();
                GameUI.m_Instance.m_TopBar.SetSimSpeedLabel(Bridge.GetTimeScaleForSimulation());
                Profile.m_PointOfViewType = PointOfViewType.SIM_CENTER;
            }
            GameStateManager.SwitchToStateImmediate(GameState.SIM);
            if (watch != null)
                PointsOfView.SnapTo(PointOfViewType.SIM_CENTER);
            activeCase = caseId;
            deadline = Time.realtimeSinceStartup + (watch == null ? FastWallSeconds : WatchWallSeconds);
            activeId = id;
        }
        catch (DesignException ex)
        {
            // Validation never advances physics; discard partial placement too.
            var request = JObject.Parse(File.ReadAllText(claimed));
            if ((string)request["action"] == "validate")
            {
                string caseId = (string)request["case"];
                LoadScenario(caseId == "gap4" ? 4 : 8, caseId == "gap4" ? 6500 : 15000, caseId);
            }
            WriteJson(id + ".result.json", new { status = "invalid_design", error = ex.Message });
        }
        catch (Exception ex)
        {
            feedback = null;
            Logger.LogError(ex);
            WriteJson(id + ".result.json", new { status = "host_error", error = ex.Message });
        }
    }

    private Dictionary<string, BridgeJoint> LoadScenario(int span, int budget, string caseId)
    {
        if (GameStateManager.GetState() == GameState.SIM)
            GameStateManager.SwitchToStateImmediate(GameState.SANDBOX);
        GameManager.SetGameMode(GameMode.SANDBOX);
        GameManager.m_Offline = true;
        var randomState = UnityEngine.Random.state;
        try
        {
            UnityEngine.Random.InitState(17171);
            Sandbox.StartNewSandbox(GameSettings.DefaultTheme().m_ThemeName);
        }
        finally
        {
            UnityEngine.Random.state = randomState;
        }
        var left = TerrainIslands.GetLeftTerrain();
        var right = TerrainIslands.GetRightTerrain();
        if (!left || !right)
            throw new InvalidOperationException("Sandbox bookends are missing");
        right.transform.position = new Vector3(span, 0, 0);
        right.UpdatePolygonShapes();
        WaterBlocks.UpdateManual();
        BridgeJoints.DestroyAllAnchors();
        var anchors = new Dictionary<string, BridgeJoint> {
            { "L", BridgeJoints.CreateAnchor(new Vector3(0, 5, 0), "benchmark-left") },
            { "R", BridgeJoints.CreateAnchor(new Vector3(span, 5, 0), "benchmark-right") }
        };
        foreach (var anchor in anchors.Values)
            anchor.m_SandboxItem = SandboxItems.AddSandboxItemComponent(anchor.gameObject, SandboxItemType.ANCHOR);
        var spawn = left.m_SpawnPoint.transform;
        var vehicle = Vehicles.CreateVehicle(Prefabs.m_Instance.m_CompactCar, spawn.position, spawn.rotation, "benchmark-car");
        vehicle.m_SandboxItem = SandboxItems.AddSandboxItemComponent(vehicle.gameObject, SandboxItemType.VEHICLE);
        var goal = SandboxItems.CreateGoalTriggerForVehicle(vehicle);
        if (!goal)
            throw new InvalidOperationException("Could not create the victory flag");
        EventTimelines.m_Timelines[0].AddStage().AddUnit(vehicle.gameObject, EventUnitType.VEHICLE);
        Budget.m_CashBudget = budget;
        Sandbox.m_CurrentLayoutName = caseId;
        if (GameStateManager.GetState() != GameState.SANDBOX)
            GameStateManager.SwitchToStateImmediate(GameState.SANDBOX);
        if (watch != null)
            FrameScenario(caseId, span, spawn.position, goal.transform.position);
        return anchors;
    }

    private void FrameScenario(string caseId, int span, Vector3 start, Vector3 goal)
    {
        float left = Mathf.Min(0, start.x, goal.x) - 1.5f;
        float right = Mathf.Max(span, start.x, goal.x) + 1.5f;
        float bottom = Mathf.Min(0, start.y, goal.y) - 0.5f;
        float top = Mathf.Max(10, start.y, goal.y) + 0.5f;
        var pivot = new Vector3((left + right) / 2f, (bottom + top) / 2f, 0);
        var position = pivot - Vector3.forward * GameSettings.CamDistFromPivot();
        var camera = Cameras.MainCamera();
        float size = Mathf.Max((top - bottom) / 2f, (right - left) / (2f * camera.aspect));
        foreach (var view in new[] { PointOfViewType.BUILD_CUSTOM, PointOfViewType.SIM_CENTER, PointOfViewType.SIM_CUSTOM })
            PointsOfView.Set(view, pivot, position, Quaternion.identity, size);
        PointsOfView.SnapTo(PointOfViewType.BUILD_CUSTOM);
        Logger.LogInfo(String.Format(CultureInfo.InvariantCulture,
            "Watch framing {0}: {1}x{2} {3}, aspect {4:0.####}, orthographic size {5:0.####}",
            caseId, Screen.width, Screen.height, Screen.fullScreenMode, camera.aspect, camera.orthographicSize));
    }

    private static List<SimulationFeedback.Member> ApplyDesign(JObject design, Dictionary<string, BridgeJoint> joints, int span)
    {
        if (design == null)
            throw new DesignException("Expected a design object");
        var jointRows = design["joints"] as JArray;
        var edgeRows = design["edges"] as JArray;
        if (jointRows == null || edgeRows == null || jointRows.Count > 48 || edgeRows.Count > 96)
            throw new DesignException("Expected joints and edges arrays with at most 48 joints and 96 edges");
        for (int index = 0; index < jointRows.Count; index++)
        {
            JArray row = jointRows[index] as JArray;
            if (row == null || row.Count != 3 || row[0].Type != JTokenType.String ||
                !IsNumber(row[1]) || !IsNumber(row[2]))
                throw new DesignException("Joint row " + (index + 1) + " was "
                    + jointRows[index].ToString(Formatting.None)
                    + ". Use [string id, numeric x, numeric y], for example [\"J1\",1.5,4].");
            string name = (string)row[0];
            double x = (double)row[1];
            double y = (double)row[2];
            string joint = DescribeJoint(name, x, y);
            if (String.IsNullOrWhiteSpace(name) || name.Length > 24)
                throw new DesignException(joint + " needs a nonblank ID of 1 to 24 characters.");
            if (joints.ContainsKey(name))
                throw new DesignException(joint + " reuses an existing ID. Reference existing IDs in edges.");
            if (!(x >= 0 && x <= span && y >= 0 && y <= 10))
                throw new DesignException(joint + " is outside the build bounds x=[0," + span + "], y=[0,10].");
            var pos = new Vector3((float)x, (float)y, 0);
            var overlap = joints.FirstOrDefault(entry =>
                Vector3.Distance(entry.Value.transform.position, pos) < GameSettings.NodeDiameter());
            if (overlap.Key != null)
            {
                Vector3 other = overlap.Value.transform.position;
                string advice = overlap.Key == "L" || overlap.Key == "R"
                    ? " Use this fixed anchor ID in edges, or move the new joint clear of the anchor."
                    : " Reuse one joint ID or move the new joint clear.";
                throw new DesignException(joint + " overlaps " + DescribeJoint(overlap.Key, other.x, other.y) + "." + advice);
            }
            joints.Add(name, BridgeJoints.CreateJoint(pos, Utils.GenerateUniqueId()));
        }
        var links = new HashSet<Tuple<string, string>>();
        var members = new List<SimulationFeedback.Member>();
        foreach (var item in edgeRows)
        {
            JArray row = item as JArray;
            if (row == null || row.Count != 3 || row.Any(v => v.Type != JTokenType.String))
                throw new DesignException("Edge row " + item.ToString(Formatting.None)
                    + " must contain three strings [from_id,to_id,material], for example [\"L\",\"J1\",\"road\"].");
            string a = (string)row[0];
            string b = (string)row[1];
            string material = (string)row[2];
            BridgeMaterialType type;
            if (a == b || !joints.ContainsKey(a) || !joints.ContainsKey(b) || !Materials.TryGetValue(material, out type))
                throw new DesignException("Unknown joint or material in edge " + a + "-" + b + " " + material);
            var pair = String.CompareOrdinal(a, b) < 0 ? Tuple.Create(a, b) : Tuple.Create(b, a);
            if (!links.Add(pair))
                throw new DesignException("Duplicate edge " + a + "-" + b);
            if (!BridgeEdges.CanFormEdgeBetweenJoints(null, joints[a], joints[b], type))
                throw new DesignException("Game rejected edge " + a + "-" + b + " " + material
                    + " (length " + Vector3.Distance(joints[a].transform.position, joints[b].transform.position).ToString("0.###")
                    + " m, material limit " + BridgeMaterials.GetMaxEdgeLength(type).ToString("0.###") + " m).");
            var edge = BridgeEdges.CreateEdge(joints[a], joints[b], type, Utils.GenerateUniqueId(), null);
            if (!edge)
                throw new InvalidOperationException("Could not place a validated edge");
            Budget.AdjustBudgetForAddedEdge(edge);
            members.Add(new SimulationFeedback.Member(members.Count + 1, a, b, material, edge));
        }
        return members;
    }

    private static void SamplePhysics(PolyPhysics.World __instance)
    {
        if (feedback != null && Bridge.IsSimulating() && __instance == Main.m_Instance.m_World)
            feedback.Sample(__instance);
    }

    private static void CaptureBreak(PolyPhysics.BridgeEdgeListener __instance, EdgeHandle e)
    {
        if (feedback != null && __instance.allowBreaking)
            feedback.RecordBreak(e, __instance.debrisSize);
    }

    private void Finish(string status)
    {
        WriteJson(activeId + ".trace.json", new {
            sample_interval_s = 0.05, signed_stress_source = "EdgeHandle.stressNormalizedSigned",
            samples = feedback.Trace
        });
        var result = new {
            status, @case = activeCase, cost,
            broken_members = GameStateSim.m_NumBridgeBreaks,
            max_stress_pct = Mathf.Round(StressSamples.m_MaxStressNormalized * 10000) / 100,
            reached_goal = Vehicles.m_Vehicles[0].m_ReachedVictoryFlag,
            simulated_seconds = feedback.simulated_seconds,
            vehicle_position = XY(Vehicles.m_Vehicles[0].transform.position),
            members = feedback.Members,
            break_events = feedback.Breaks
        };
        feedback = null;
        string id = activeId;
        activeId = null;
        try
        {
            if (watch == null)
                GameStateManager.SwitchToStateImmediate(GameState.SANDBOX);
            else
            {
                GameStateSim.Pause();
                holding = true;
            }
            WriteJson(id + ".result.json", result);
        }
        catch (Exception ex)
        {
            Logger.LogError(ex);
            WriteJson(id + ".result.json", new { status = "host_error", error = ex.Message });
        }
    }

    private static bool IsNumber(JToken value)
    {
        return value.Type == JTokenType.Float || value.Type == JTokenType.Integer;
    }

    private static string DescribeJoint(string name, double x, double y)
    {
        return String.Format(CultureInfo.InvariantCulture, "Joint {0} at ({1:0.###}, {2:0.###})",
            JsonConvert.ToString(name), x, y);
    }

    private static float[] XY(Vector3 position)
    {
        return new[] { position.x, position.y };
    }

    private static void WriteJson(string name, object value)
    {
        string path = Path.Combine(inbox, name);
        string temporary = path + ".tmp";
        File.WriteAllText(temporary, JsonConvert.SerializeObject(value));
        File.Move(temporary, path);
    }

    private sealed class DesignException : Exception
    {
        public DesignException(string message) : base(message) {}
    }
}
