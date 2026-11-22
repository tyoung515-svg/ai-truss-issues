using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json;
using Poly.Physics;
using UnityEngine;

internal sealed class SimulationFeedback
{
    internal sealed class Member
    {
        public int member { get; private set; }
        public string from { get; private set; }
        public string to { get; private set; }
        public string material { get; private set; }
        public float? peak_stress_pct { get; private set; }
        public float? peak_at_s { get; private set; }
        public bool broken { get; set; }

        [JsonIgnore]
        public BridgeEdge Edge { get; private set; }

        public Member(int index, string a, string b, string material, BridgeEdge edge)
        {
            member = index;
            from = a;
            to = b;
            this.material = material;
            Edge = edge;
        }

        public float? Sample(float stress, float time)
        {
            if (float.IsNaN(stress) || float.IsInfinity(stress))
                return null;
            float percent = Mathf.Round(stress * 10000) / 100;
            if (!peak_stress_pct.HasValue || percent > peak_stress_pct.Value)
            {
                peak_stress_pct = percent;
                peak_at_s = Seconds(time);
            }
            return percent;
        }
    }

    private readonly Dictionary<BridgeEdge, Member> byEdge;
    private readonly Dictionary<string, BridgeJoint> joints;
    private float nextSample;
    private float globalPeak;
    public List<object> Trace { get; private set; }
    public List<Member> Members { get; private set; }
    public List<object> Breaks { get; private set; }
    public float simulated_seconds { get; private set; }

    public SimulationFeedback(List<Member> members, Dictionary<string, BridgeJoint> joints)
    {
        this.joints = joints;
        Trace = new List<object>();
        Members = members;
        byEdge = members.ToDictionary(member => member.Edge);
        Breaks = new List<object>();
    }

    public void Sample(PolyPhysics.World world)
    {
        simulated_seconds = Seconds(world.timeElapsed);
        bool newPeak = false;
        foreach (Member member in Members)
        {
            if (member.broken || !member.Edge)
                continue;
            var edge = member.Edge.m_PhysicsEdge;
            if (edge && edge.handle && edge.handle.world == world)
            {
                float? percent = member.Sample(edge.handle.stressNormalized, world.timeElapsed);
                if (percent.HasValue && percent.Value > globalPeak)
                {
                    globalPeak = percent.Value;
                    newPeak = true;
                }
            }
        }
        if (world.timeElapsed >= nextSample)
        {
            Snapshot(world.timeElapsed, "scheduled", null);
            nextSample = (Mathf.Floor(world.timeElapsed / 0.05f) + 1) * 0.05f;
        }
        else if (newPeak)
            Snapshot(world.timeElapsed, "new_global_peak", null);
    }

    private void Snapshot(float time, string reason, float? eventStress)
    {
        var positions = new Dictionary<string, float[]>();
        foreach (var pair in joints)
        {
            var joint = pair.Value;
            if (!joint) continue;
            positions[pair.Key] = joint.m_PhysicsNode && joint.m_PhysicsNode.handle
                ? XY(joint.m_PhysicsNode.handle.pos) : XY(joint.transform.position);
        }
        Trace.Add(new {
            time_s = Seconds(time),
            sample_reason = reason, event_stress_pct = eventStress,
            car = XY(Vehicles.m_Vehicles[0].transform.position),
            joints = positions,
            members = Members.Select(member => {
                var edge = member.Edge ? member.Edge.m_PhysicsEdge : null;
                bool live = !member.broken && edge && edge.handle;
                return new {
                    member = member.member, broken = member.broken,
                    stress_pct = live ? Percent(edge.handle.stressNormalized) : null,
                    signed_stress_pct = live ? Percent(edge.handle.stressNormalizedSigned) : null
                };
            }).ToArray()
        });
    }

    private static float? Percent(float stress)
    {
        return float.IsNaN(stress) || float.IsInfinity(stress)
            ? (float?)null : Mathf.Round(stress * 10000) / 100;
    }

    public void RecordBreak(EdgeHandle edge, float debrisSize)
    {
        if (!edge.unityEdgeComponent)
            return;
        var original = edge.unityEdgeComponent.userData as BridgeEdge;
        Member member;
        if (!original || !byEdge.TryGetValue(original, out member) || member.broken)
            return;

        // The native listener creates the two debris endpoints at these positions.
        Vector2 a = edge.node0.pos;
        Vector2 b = edge.node1.pos;
        Vector2 first = Vector2.Lerp(a, b, debrisSize);
        Vector2 second = Vector2.Lerp(a, b, 1f - debrisSize);
        float time = edge.world.timeElapsed + Time.fixedDeltaTime;
        float? stress = member.Sample(edge.stressNormalized, time);
        member.broken = true;
        Breaks.Add(new {
            member = member.member, time_s = Seconds(time), stress_pct = stress,
            points = first == second ? new[] { XY(first) } : new[] { XY(first), XY(second) }
        });
        Snapshot(time, "break", stress);
    }

    private static float Seconds(float value)
    {
        return Mathf.Round(value * 1000) / 1000;
    }

    private static float[] XY(Vector2 position)
    {
        return new[] { Seconds(position.x), Seconds(position.y) };
    }
}
