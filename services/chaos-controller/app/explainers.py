"""
chaos-controller/app/explainers.py

Educational metadata for each chaos action + composed scenarios.
See the original file header for the schema.
"""

EXPLAINERS = {
    'kill_pod': {
        'title': 'A pod was killed',
        'summary': 'One running pod was deleted. The system replaced it within seconds, with no customer-visible outage.',
        'what_happens': [
            'Pod is marked Terminating. The kubelet sends SIGTERM to its containers.',
            'The readiness probe stops returning success — usually within a second or two.',
            "The Service's EndpointSlice removes that pod. Traffic to the service routes only to surviving pods.",
            'The ReplicaSet controller notices replicas < desired and creates a replacement.',
            'The scheduler places the new pod. Image is pulled (cached, fast). Container starts.',
            'The readiness probe begins passing again. The EndpointSlice adds the new pod. Traffic resumes.'
        ],
        'primitives': [
            ('Liveness Probe', 'Restarts a container that has stopped responding.'),
            ('Readiness Probe', 'Gates traffic — only Ready pods receive requests.'),
            ('Service / EndpointSlice', 'Decouples client traffic from individual pod IPs.'),
            ('ReplicaSet', 'Keeps the desired number of pods running.'),
            ('Rolling update strategy', 'Ensures replacements happen one-at-a-time.')
        ],
        'learn_more': [
            ('Probes (Kubernetes docs)', 'https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/'),
            ('Services & Endpoints', 'https://kubernetes.io/docs/concepts/services-networking/service/')
        ],
        'takeaway': "Pods are cattle, not pets. Kubernetes treats every pod as replaceable — that's the whole point.",
    },
    'cpu_pressure': {
        'title': 'CPU load spiked',
        'summary': 'A service hit high CPU usage. The Horizontal Pod Autoscaler responded by adding replicas, and latency recovered without anyone paging.',
        'what_happens': [
            'CPU usage on the service climbs past its target (typically 70%).',
            'Latency p95 begins to climb as requests queue.',
            'The HorizontalPodAutoscaler reads the metric every 15-30s and decides to scale.',
            'New pods are created. The scheduler places them on nodes with headroom.',
            'Load balances across the bigger fleet. Latency returns to baseline.',
            "Once load drops, HPA's stabilization window prevents flapping — it waits before scaling back down."
        ],
        'primitives': [
            ('HorizontalPodAutoscaler', 'Scales replica count based on metrics like CPU or custom signals.'),
            ('Cluster Autoscaler', "Adds nodes when pods can't be scheduled due to resource constraints."),
            ('Resource Requests/Limits', 'Tells the scheduler how much each pod needs.'),
            ('Pod Affinity / Spread', 'Distributes new pods across nodes for better fault tolerance.')
        ],
        'learn_more': [
            ('HPA (Kubernetes docs)', 'https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/'),
            ('Cluster Autoscaler', 'https://github.com/kubernetes/autoscaler')
        ],
        'takeaway': 'Autoscaling beats over-provisioning. Pay for what you use, scale up only when load demands it.',
    },
    'network_partition': {
        'title': 'Network traffic was cut to a service',
        'summary': 'Traffic to a downstream service was blocked. The circuit breaker tripped and fast-failed requests until the partition healed.',
        'what_happens': [
            'Requests start timing out — initially they hang waiting for replies that never come.',
            'After a small number of consecutive failures, the circuit breaker enters OPEN state.',
            'All subsequent requests fail fast (no timeout wait) until the half-open trial.',
            'The system periodically sends a single test request to see if the downstream has recovered.',
            'Once a test request succeeds, the breaker moves to HALF-OPEN, then to CLOSED.',
            'Normal traffic resumes.'
        ],
        'primitives': [
            ('Circuit Breaker', 'Prevents cascading failure by failing fast when a downstream is dead.'),
            ('Retry with backoff', 'Handles transient failures without piling on a struggling service.'),
            ('Bulkhead pattern', "Isolates resources so one slow service doesn't exhaust your thread pool."),
            ('NetworkPolicy', 'Defines which pods can talk to which — your enforcement layer.')
        ],
        'learn_more': [
            ('Circuit Breaker pattern', 'https://martinfowler.com/bliki/CircuitBreaker.html'),
            ('NetworkPolicy', 'https://kubernetes.io/docs/concepts/services-networking/network-policies/')
        ],
        'takeaway': 'Fail fast instead of hanging. A circuit breaker turns a 30-second timeout into a 30-millisecond rejection.',
    },
    'expire_jwt': {
        'title': 'JWT signing keys were rotated',
        'summary': 'The active JWT signing key was rotated. Clients re-authenticated through the refresh-token flow without any user-visible logout.',
        'what_happens': [
            'The user-service is configured with a key ring: multiple (kid, secret) pairs. It signs new JWTs with the current kid.',
            'Operators add a new key to the ring (k2) and roll user-service. New tokens are now signed with k2.',
            'The api-gateway still trusts the old kid (k1) until the rotation is complete.',
            'After the JWT expiry window (e.g. 60 min), all k1 tokens have naturally expired. Clients have refreshed to k2.',
            'Operators remove k1 from the trust list. k1 secret is no longer valid.',
            'If a leak ever occurred, the blast radius is now bounded by the expiry window plus the time to detect.'
        ],
        'primitives': [
            ('JWT keyring (kid header)', "The token's JOSE header includes the key id that signed it."),
            ('Refresh tokens', 'Long-lived opaque tokens that mint new JWTs without re-authenticating.'),
            ('Key rotation choreography', 'Add new key → switch signer → wait → remove old key.')
        ],
        'learn_more': [
            ('RFC 7515 (JOSE Header)', 'https://datatracker.ietf.org/doc/html/rfc7515#section-4.1.4'),
            ('OAuth 2.0 Token Refresh', 'https://datatracker.ietf.org/doc/html/rfc6749#section-6')
        ],
        'takeaway': 'Single secrets age forever. Keyrings let you rotate without mass-logout — and limit blast radius after a leak.',
    },
    'region_outage': {
        'title': 'An availability zone went down',
        'summary': 'Half the cluster nodes were marked NotReady. Traffic shifted to the surviving AZ within seconds, and the autoscaler provisioned replacement capacity.',
        'what_happens': [
            'Nodes in the failed zone are marked NotReady. Their pods become unreachable.',
            'EndpointSlices update to exclude pods on those nodes. Traffic shifts to surviving zones.',
            'Replica counts on each Deployment drop below desired.',
            'The ReplicaSet controller tries to schedule replacements. If existing nodes have capacity, pods come up fast.',
            'If not, the Cluster Autoscaler provisions new nodes in the surviving AZ.',
            "Topology spread constraints ensure new pods don't all land on the same node."
        ],
        'primitives': [
            ('Multi-AZ replication', "Run replicas across multiple zones so one zone outage isn't catastrophic."),
            ('Topology Spread Constraints', 'Ensure pods are distributed across failure domains.'),
            ('Pod Anti-Affinity', 'Prevent multiple replicas of the same service from co-locating.'),
            ('Cluster Autoscaler', 'Adds replacement nodes when capacity is short.'),
            ('Pod Disruption Budget', 'Prevents voluntary disruptions from compounding the outage.')
        ],
        'learn_more': [
            ('Topology Spread Constraints', 'https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/'),
            ('Multi-zone clusters', 'https://kubernetes.io/docs/setup/best-practices/multiple-zones/')
        ],
        'takeaway': 'A single AZ is a single point of failure. Spread your workload across zones — or expect downtime when one goes down.',
    },
    'bad_deploy': {
        'title': 'A broken deployment was rolled out',
        'summary': 'A new image failed its readiness probe. The rolling update halted automatically and the rollout was reverted to the last known-good version.',
        'what_happens': [
            'A new ReplicaSet is created for the new image version.',
            'The first new pod tries to start. Image pulls succeed but readiness probe fails (e.g. /health/ready returns 500).',
            'Rolling update strategy has maxUnavailable=0, so the rollout pauses rather than removing healthy old pods.',
            'After progressDeadlineSeconds (default 600s), the Deployment is marked as failed.',
            'Either an operator runs `kubectl rollout undo` or a controller (Argo Rollouts, Flagger) auto-rolls back.',
            'The new ReplicaSet is scaled to zero. The old one is unchanged. Service stays up the whole time.'
        ],
        'primitives': [
            ('Readiness Probe', 'The gate that determines if a new version is ready for traffic.'),
            ('Rolling Update strategy', 'maxSurge / maxUnavailable controls how aggressively to replace old pods.'),
            ('progressDeadlineSeconds', 'How long to wait before declaring a rollout failed.'),
            ('Canary deployment', 'Roll out to a small subset of traffic first — catch bad deploys with low blast radius.'),
            ('Argo Rollouts / Flagger', 'Progressive delivery controllers with auto-rollback on metrics.')
        ],
        'learn_more': [
            ('Deployments (Kubernetes docs)', 'https://kubernetes.io/docs/concepts/workloads/controllers/deployment/'),
            ('Argo Rollouts', 'https://argoproj.github.io/argo-rollouts/')
        ],
        'takeaway': 'Readiness probes catch bad deploys before they hit users. Rolling updates with maxUnavailable=0 mean a broken deploy is loud, not silent.',
    },
    'redis_failure': {
        'title': 'Redis became unavailable',
        'summary': 'The Redis cache failed. Rate limiting fell back to in-memory mode, and downstream calls bypassed the cache layer.',
        'what_happens': [
            "The api-gateway's rate limiter detects Redis is unreachable.",
            'It falls back to an in-memory bucket — losing cross-replica coordination but staying available.',
            'Cache-dependent endpoints see slower response times because they hit the database directly.',
            'Sessions stored in Redis are unavailable. If the system uses cache-aside, reads fall through to the DB.',
            'Once Redis recovers, the rate limiter switches back to distributed mode.',
            'Cold cache is rebuilt naturally as traffic flows through.'
        ],
        'primitives': [
            ('Cache fallback', 'Always have a path to the source of truth — the cache is an optimization, not a dependency.'),
            ('Rate limiter with in-memory fallback', 'Better to overcount than to drop traffic completely.'),
            ('Cache-aside pattern', 'App reads cache → falls through to DB on miss → writes back.'),
            ('Connection pool with retries', 'Reduces the impact of brief outages.')
        ],
        'learn_more': [
            ('Redis Sentinel/Cluster (HA)', 'https://redis.io/docs/management/sentinel/'),
            ('Cache patterns (AWS)', 'https://aws.amazon.com/caching/best-practices/')
        ],
        'takeaway': 'Treat the cache as optional. The day your cache dies should not be the day your system dies.',
    },
    'db_failure': {
        'title': 'The database became unreachable',
        'summary': 'The primary database connection failed. Reads degraded to read-replicas; writes failed fast with 503s. The pool reconnected when the primary came back.',
        'what_happens': [
            'Write requests start failing as connections drop.',
            'The connection pool retries with exponential backoff. If the DB is truly down, retries waste latency.',
            '503 Service Unavailable surfaces to clients with a Retry-After header.',
            'If a read replica exists, the read path stays partially available — degraded but not fully down.',
            'When the primary recovers, the pool re-establishes connections. Traffic resumes.',
            "If this happens often, you'd add a circuit breaker around DB calls to fail faster."
        ],
        'primitives': [
            ('Connection pooling', 'Reuses connections — and re-establishes them when they drop.'),
            ('Read replicas', 'Read-heavy workloads survive primary outages.'),
            ('Retry with backoff', 'Handles transient failures without thundering herd on recovery.'),
            ('Circuit breaker on DB', 'Prevents request queue blow-up during sustained outage.'),
            ('503 + Retry-After', 'Tells well-behaved clients exactly when to come back.')
        ],
        'learn_more': [
            ('PgBouncer (connection pooling)', 'https://www.pgbouncer.org/'),
            ('503 Retry-After (MDN)', 'https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503')
        ],
        'takeaway': 'Your database WILL go down. Plan for it: read replicas, connection pools, retries, and honest 503s.',
    },
    'slow_network': {
        'title': 'Network latency was injected',
        'summary': 'Every inter-service call gained 500ms of latency. Timeouts and queues built up. The system would need circuit breakers and bulkheads to survive sustained latency.',
        'what_happens': [
            'p95 latency climbs proportionally to the injected delay.',
            'Connection pools fill up — every connection takes longer to free.',
            'Without bulkheads, one slow downstream can starve threads that other endpoints need.',
            "Timeouts trigger if they're set tighter than the added latency. 504 Gateway Timeout responses begin appearing.",
            'Retries amplify the problem — the same slow request now happens N times instead of once.',
            'Once latency returns to normal, queues drain. Watch for thundering-herd as queued requests all fire at once.'
        ],
        'primitives': [
            ('Bulkhead pattern', "Isolate connection pools per-downstream so one slow service can't take down others."),
            ('Timeouts everywhere', 'Every network call must have a timeout. No exceptions.'),
            ('Adaptive concurrency limits', 'Shed load when latency climbs, before timeouts cascade.'),
            ('Request hedging', 'Send the same request to two replicas; take the first response.')
        ],
        'learn_more': [
            ('Bulkhead pattern', 'https://learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead'),
            ('Adaptive concurrency (Netflix)', 'https://github.com/Netflix/concurrency-limits')
        ],
        'takeaway': 'Slow is the new down. A service that takes 30 seconds to respond is worse than one that returns 500 in 30ms.',
    },
    'memory_leak': {
        'title': 'A pod is leaking memory',
        'summary': "A pod's memory usage climbed steadily until it hit its limit. The kubelet OOM-killed the container, the readiness probe failed, traffic moved to other replicas, and the pod restarted.",
        'what_happens': [
            'Memory usage climbs over time, well above normal.',
            'It crosses the resource limit. The kubelet receives an OOM kill signal from the kernel.',
            'The container exits. The pod enters CrashLoopBackOff if it keeps happening.',
            'Readiness probe fails. EndpointSlice removes the pod from service.',
            'ReplicaSet replaces it (or restart policy restarts the container).',
            'If this keeps happening, the operator sees the CrashLoop pattern in alerts. They roll back or fix the leak.'
        ],
        'primitives': [
            ('Resource Limits (memory)', 'Caps how much memory a pod can use — kernel enforces.'),
            ('Quality of Service classes', 'Guaranteed pods are killed last; Best-Effort first.'),
            ('OOMKill detection', 'Distinguishes process exit from kernel OOM.'),
            ('Vertical Pod Autoscaler', 'Auto-tunes resource requests based on actual usage.'),
            ('Memory profiling in prod', 'pprof, parca, py-spy — find the leak before it kills you.')
        ],
        'learn_more': [
            ('QoS Classes', 'https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/'),
            ('VPA (Vertical Pod Autoscaler)', 'https://github.com/kubernetes/autoscaler/tree/master/vertical-pod-autoscaler')
        ],
        'takeaway': 'Set memory limits. The OOM killer is your friend — it bounds the blast radius of a leaking pod to that pod.',
    },
    'cert_expiry': {
        'title': 'A TLS certificate expired',
        'summary': 'The TLS cert for an internal service expired. New connections failed with x509 errors. cert-manager noticed and rotated to a fresh cert within the renewal window.',
        'what_happens': [
            "The cert's notAfter date passes. New TLS handshakes fail with x509 certificate expired errors.",
            'Existing connections (already in TCP keep-alive) may continue working briefly.',
            'All cross-service calls start returning TLS errors.',
            "If cert-manager (or similar) is running, it should have renewed at notAfter - 30 days. If it didn't, alerts fire.",
            'Operator forces renewal. New cert distributes to all replicas (Secret update + reload).',
            'Connections succeed again. Lesson: monitor cert age, not just whether the service is up.'
        ],
        'primitives': [
            ('cert-manager', 'Kubernetes-native cert lifecycle: issue, renew, distribute.'),
            ('Mutual TLS (mTLS)', 'Service-to-service identity via certificates.'),
            ('Service Mesh', 'Istio/Linkerd handle cert rotation for you transparently.'),
            ('Cert monitoring (Prometheus)', 'Alert when a cert is within N days of expiry.')
        ],
        'learn_more': [
            ('cert-manager', 'https://cert-manager.io/'),
            ("Let's Encrypt", 'https://letsencrypt.org/')
        ],
        'takeaway': "Certs always expire. Don't get caught by surprise — automate renewal, alert on age, test rotation.",
    },
    'cascading_failure': {
        'title': 'A failure cascaded across services',
        'summary': 'A failure in one service caused dependent services to fail too. Without bulkheads and circuit breakers, the outage would have spread further.',
        'what_happens': [
            'Service A goes down (any reason — OOM, crash, network).',
            "Service B depends on A. Its requests to A time out. B's connection pool fills with hung requests.",
            "B's response times climb. B's own clients start timing out.",
            'Service C depends on B. The blast radius is now three services wide.',
            'Circuit breakers in B and C trip, isolating the failure to A. B and C degrade gracefully.',
            'When A recovers, the breakers transition through HALF-OPEN to CLOSED.'
        ],
        'primitives': [
            ('Circuit Breakers (everywhere)', 'Per downstream, per pool. Fail fast instead of queueing.'),
            ('Bulkheads', "Separate connection pools per downstream so one bad pool can't starve others."),
            ('Graceful degradation', 'Return a degraded but useful response when a non-critical dep is down.'),
            ('Backpressure', "Slow yourself down when downstream is slow — don't pile on."),
            ('Chaos engineering', 'Find the cascade paths in staging before production does.')
        ],
        'learn_more': [
            ('Release It! (Michael Nygard)', 'https://pragprog.com/titles/mnee2/release-it-second-edition/'),
            ('Resilience Engineering (SRE Book)', 'https://sre.google/sre-book/handling-overload/')
        ],
        'takeaway': "Failures don't stop on their own. Without circuit breakers, every dependency is a cascade waiting to happen.",
    },
    'dns_failure': {
        'title': 'DNS resolution failed',
        'summary': "DNS lookups for in-cluster service names began failing. Connections couldn't be established. CoreDNS recovered (or the cached entries kept working) and traffic resumed.",
        'what_happens': [
            'CoreDNS pods become unresponsive (overloaded, crashed, or partitioned).',
            "New DNS lookups in pods return SERVFAIL. New connections can't resolve service names.",
            'Existing connections continue (they were resolved before the failure).',
            "Apps with DNS caching (e.g. Java's default forever-cache) might keep working briefly.",
            'Apps without caching see immediate errors. Liveness probes that use hostnames may start failing.',
            'CoreDNS pods are replaced. DNS resolution recovers.'
        ],
        'primitives': [
            ('CoreDNS HA', 'Always run multiple CoreDNS replicas across nodes.'),
            ('NodeLocal DNSCache', 'Per-node DNS caching dramatically reduces load on CoreDNS.'),
            ('DNS caching in apps', "Cache positive results for a few seconds. Don't cache forever (Java)."),
            ('Use IPs in critical paths', 'For ultra-critical inter-service calls, consider resolving once and caching IP.')
        ],
        'learn_more': [
            ('CoreDNS', 'https://coredns.io/'),
            ('NodeLocal DNSCache', 'https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/')
        ],
        'takeaway': "DNS is a dependency. Cache it, replicate it, and don't forget about it — it's invisible until it breaks.",
    },
    'disk_full': {
        'title': "A pod's disk filled up",
        'summary': "A pod's ephemeral storage (or attached PV) filled up. Writes failed. The kubelet evicted the pod once it crossed the eviction threshold.",
        'what_happens': [
            'Application logs verbosely (or temp files accumulate). Disk usage climbs.',
            'Writes start failing with ENOSPC. The app starts returning 500s for write paths.',
            "kubelet sees the node's ephemeral storage usage cross the soft eviction threshold.",
            'kubelet picks pods to evict — Best-Effort QoS first, then Burstable.',
            "Pod is evicted with reason 'DiskPressure'. ReplicaSet creates a replacement.",
            'On a fresh disk, writes succeed again. Underlying cause (logging, temp files) still needs fixing.'
        ],
        'primitives': [
            ('Ephemeral Storage Limits', "Cap a pod's local disk use. Kubelet enforces."),
            ('Volume Quotas', 'PV-level limits, not just node-level.'),
            ('Log Rotation', 'logrotate / sidecar — bounded log files.'),
            ('Eviction Thresholds', 'Node-level kubelet config: soft + hard thresholds for memory, disk, inodes.'),
            ('QoS Classes', 'Guaranteed evicted last, Best-Effort first.')
        ],
        'learn_more': [
            ('Node-pressure Eviction', 'https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/'),
            ('Ephemeral storage', 'https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/#local-ephemeral-storage')
        ],
        'takeaway': "Set ephemeral-storage limits and rotate your logs. The node's disk is a shared resource — fill it and the kubelet evicts everyone.",
    },
    'gc_pause': {
        'title': 'Garbage collection paused a service',
        'summary': 'A long GC pause (~600ms) on a single replica caused latency spikes. Load balancer noticed and routed around the slow pod.',
        'what_happens': [
            'Memory usage climbs past the heap threshold. JVM (or Go, .NET, etc.) triggers a major GC cycle.',
            'GC pauses the application threads for hundreds of milliseconds.',
            'All in-flight requests on that pod stall. p99 latency spikes sharply.',
            'Outlier-based load balancing (Envoy, Istio) detects the slow pod and downweights it temporarily.',
            'Once GC completes, latency returns to baseline. The pod is gradually re-added to the rotation.',
            'Long-term fix: tune heap, switch GC algorithm (G1 → ZGC), or migrate to a lower-pause runtime.'
        ],
        'primitives': [
            ('Outlier Detection', 'Envoy/Istio remove slow upstreams from the pool temporarily.'),
            ('Request Hedging', 'Send the same request to multiple replicas; take the first response.'),
            ('p99 latency monitoring', 'Track tail latency, not just average — averages hide GC pauses.'),
            ('JVM tuning', 'Heap sizing, GC algorithm, GC pause SLOs.')
        ],
        'learn_more': [
            ('Envoy Outlier Detection', 'https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/outlier'),
            ('ZGC (Z Garbage Collector)', 'https://wiki.openjdk.org/display/zgc/Main')
        ],
        'takeaway': 'Watch p99, not p50. The 1% slow tail is where users feel pain — and GC is the classic cause.',
    },
    'thundering_herd': {
        'title': 'A thundering herd hit the system',
        'summary': 'When a brief upstream outage recovered, thousands of queued retries fired at once. Without backoff jitter, the recovery itself caused a second outage.',
        'what_happens': [
            'Service A is briefly unavailable. Clients queue retries with simple exponential backoff (no jitter).',
            'Service A comes back. All retries fire at the same instant.',
            'Service A is immediately overwhelmed — load far exceeds steady-state capacity.',
            'Service A throttles or crashes again under the herd.',
            'Cycle repeats unless clients add jitter to backoff, or the service uses adaptive concurrency limiting.',
            'Long-term fix: full jitter (random delay 0 to backoff_max), token bucket rate limiter, or queue with priorities.'
        ],
        'primitives': [
            ('Backoff with full jitter', 'Random delay in [0, backoff_max] — spreads retries over time.'),
            ('Adaptive concurrency limits', "Slow yourself down when latency climbs — Netflix's concurrency-limits."),
            ('Request coalescing', 'Multiple identical requests dedupe into one upstream call.'),
            ('Rate limiter (token bucket)', 'Bounds requests per second regardless of bursts.')
        ],
        'learn_more': [
            ('AWS — Exponential Backoff and Jitter', 'https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/')
        ],
        'takeaway': 'Retries are not free. Without jitter, every retry storm becomes the next outage.',
    },
    'noisy_neighbor': {
        'title': 'A noisy neighbor stole resources',
        'summary': 'One pod on a node consumed disproportionate CPU/memory and degraded its co-tenants. The scheduler ultimately moved it to a less crowded node.',
        'what_happens': [
            'Pod X has no resource limits (or limits much larger than typical use).',
            "Under load, X consumes most of the node's CPU. Co-tenant pods get throttled.",
            'Co-tenant pods see latency spikes despite their own CPU usage being modest.',
            'Operators notice: a service is slow only on certain nodes.',
            "Setting resource limits on X caps its impact. Alternately, mark co-tenants 'Guaranteed' QoS so they're not throttled.",
            'Long-term: dedicate node pools for noisy workloads, or use vertical pod autoscaling to right-size.'
        ],
        'primitives': [
            ('Resource Limits', "Caps a pod's CPU/memory at the kernel level."),
            ('QoS Classes', 'Guaranteed pods (requests == limits) are protected from co-tenant pressure.'),
            ('Node Pools / Taints', 'Isolate noisy workloads on dedicated nodes.'),
            ('Vertical Pod Autoscaler', 'Auto-tunes requests based on actual usage.'),
            ('CPU pinning / cpu-manager-policy=static', 'Reserve specific cores for latency-sensitive pods.')
        ],
        'learn_more': [
            ('QoS Classes', 'https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/'),
            ('VPA', 'https://github.com/kubernetes/autoscaler/tree/master/vertical-pod-autoscaler')
        ],
        'takeaway': 'Always set resource limits. Without them, your noisy workloads silently steal from your good ones.',
    },
    'spot_reclaim': {
        'title': 'A spot instance was reclaimed',
        'summary': 'The cloud provider reclaimed a spot node with 30 seconds notice. Pods were drained, replacements scheduled on on-demand capacity, no customer impact.',
        'what_happens': [
            'Cloud provider sends a 30-second interrupt notice to the node (AWS spot, GCP preemptible).',
            'Node-termination-handler (or similar) receives the notice and cordons the node.',
            'kubectl drain runs: pods evicted gracefully, respecting PDBs.',
            'Pods reschedule on remaining capacity. If insufficient, cluster autoscaler provisions new nodes.',
            'Stateless services replace seamlessly. Stateful ones rely on PV reattachment.',
            'Total user impact: a single pod restart per affected service.'
        ],
        'primitives': [
            ('Pod Disruption Budget', 'Caps how many pods can be evicted simultaneously.'),
            ('Node Termination Handler', 'AWS node-termination-handler / GCP equivalent — translates cloud signals into kubectl drains.'),
            ('Graceful Shutdown (terminationGracePeriodSeconds)', 'Time given to finish in-flight work.'),
            ('preStop Hooks', 'Run cleanup logic before SIGTERM.'),
            ('Spot + On-demand mix', 'Run critical workloads on on-demand, scale-out on spot.')
        ],
        'learn_more': [
            ('AWS Node Termination Handler', 'https://github.com/aws/aws-node-termination-handler'),
            ('Spot Best Practices', 'https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-best-practices.html')
        ],
        'takeaway': 'Spot capacity is 60-90% cheaper but disappears with 30s notice. Design for it: PDBs, graceful shutdown, mixed node pools.',
    },
    'autoscaler_stuck': {
        'title': "The cluster autoscaler couldn't add capacity",
        'summary': 'Pods were Pending because no nodes had room. The cluster autoscaler tried to provision new nodes but the cloud-side capacity was exhausted in the target instance family.',
        'what_happens': [
            "Load increases. HPA scales replicas. Pods can't be scheduled — cluster is at capacity.",
            'Cluster autoscaler sees Pending pods and requests new nodes.',
            'Cloud provider returns InsufficientInstanceCapacity — the requested instance family has no capacity in the zone.',
            'Autoscaler keeps retrying. Pods stay Pending.',
            'Mitigation: configure multiple instance types in the node pool, or fall back to a different zone.',
            "Long-term: don't depend on a single instance type. Use mixed pools."
        ],
        'primitives': [
            ('Cluster Autoscaler with priority expander', 'Tries multiple node groups in priority order.'),
            ('Mixed instance pools', "Don't put all eggs in one m5.xlarge basket."),
            ('Multi-AZ node groups', 'Spread capacity across zones.'),
            ('Karpenter', 'AWS-native, smarter consolidation, faster provisioning.'),
            ('Headroom pods', 'Always-pending low-priority pods that keep extra capacity warm.')
        ],
        'learn_more': [
            ('Cluster Autoscaler FAQ', 'https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md'),
            ('Karpenter', 'https://karpenter.sh/')
        ],
        'takeaway': 'Cloud capacity is finite. Diversify your instance types and zones, or one bad day in us-east-1a takes you down.',
    },
    'api_throttle': {
        'title': 'The Kubernetes API server was overloaded',
        'summary': 'Too many concurrent kubectl/controller requests overwhelmed the API server. Priority and Fairness shed low-priority requests so critical reconciliation kept happening.',
        'what_happens': [
            'A misbehaving controller (or a tight retry loop in CI) hammers the API server with LIST requests.',
            'API server queue depth climbs. Some requests get rejected with 429 Too Many Requests.',
            'API Priority and Fairness (APF) categorizes requests: system-critical (kube-controller-manager, scheduler) are preserved.',
            "Low-priority requests (cluster-admin kubectl from a developer's terminal) get queued or rejected.",
            "Workload pods are unaffected — they don't talk to the API server. Reconciliation happens, just slower.",
            'Mitigation: identify and rate-limit the offending controller; scale API server replicas.'
        ],
        'primitives': [
            ('API Priority and Fairness', 'Workload-aware request prioritization on the API server.'),
            ('kubectl with --request-timeout', 'Avoid hammering API server with unbounded retries.'),
            ('Watch (not poll)', 'Use watch streams instead of repeated list calls.'),
            ('etcd performance tuning', 'Compaction, defragmentation, IOPS budget.')
        ],
        'learn_more': [
            ('API Priority and Fairness', 'https://kubernetes.io/docs/concepts/cluster-administration/flow-control/')
        ],
        'takeaway': 'The control plane is also a system to be protected. APF is your circuit breaker for the API server.',
    },
    'secret_leak': {
        'title': 'A secret was leaked and needs rotation',
        'summary': 'A database password was committed to a public Git repo. Operators rotated the secret across services. Old connections drained; new ones authenticated with the new secret.',
        'what_happens': [
            'Secret-scanning bot detects the leaked credential. Alerts fire to security on-call.',
            'Operator generates a new secret value. Updates the Kubernetes Secret object.',
            'Pods using projected secret volumes see the file update in seconds (no restart needed).',
            'Pods using env-var secrets need a rolling restart to pick up the new value.',
            'Database accepts both old and new credentials briefly during the cutover window.',
            'Old credential is revoked. Existing connections drain on natural reconnection.',
            'Postmortem: add pre-commit hooks, secret-scanning in CI, External Secrets Operator with a vault.'
        ],
        'primitives': [
            ('Projected secret volumes', 'Auto-update without restart.'),
            ('External Secrets Operator', 'Sync from Vault / AWS SM / GCP SM into K8s Secrets.'),
            ('Sealed Secrets', 'Encrypt secrets at rest in Git, decrypt only in-cluster.'),
            ('Secret rotation choreography', 'Add new → switch consumers → revoke old.'),
            ('Pre-commit hooks', 'gitleaks / trufflehog catch leaks before push.')
        ],
        'learn_more': [
            ('External Secrets Operator', 'https://external-secrets.io/'),
            ('Sealed Secrets', 'https://github.com/bitnami-labs/sealed-secrets')
        ],
        'takeaway': "Secrets in Git are not 'if', they're 'when'. Make rotation routine — don't make it heroic.",
    },
    'service_mesh_crash': {
        'title': 'A service mesh sidecar crashed',
        'summary': 'An Envoy sidecar OOMed on one pod. The application kept running but no traffic could reach it. The sidecar restarted and traffic resumed within 5 seconds.',
        'what_happens': [
            'Envoy sidecar exceeds its memory limit (often during a config reload with large config).',
            'kubelet OOMKills the sidecar container. Application container keeps running.',
            'Service mesh sees the sidecar as down — removes the pod from the load-balancing pool.',
            'Traffic routes only to surviving pods.',
            'Sidecar restarts (restartPolicy: Always). xDS config syncs from the control plane.',
            "Sidecar becomes ready. Traffic resumes. Total impact: a single pod's worth of capacity for ~5s.",
            'Mitigation: size sidecar limits appropriately, monitor sidecar memory separately, use ambient-mesh approaches.'
        ],
        'primitives': [
            ('Sidecar pattern (Istio/Linkerd)', 'Envoy proxy alongside every pod.'),
            ('xDS API', 'How Envoy receives config from the mesh control plane.'),
            ('Ambient mesh (Istio)', 'Mesh without per-pod sidecars — different trade-offs.'),
            ('Resource limits on sidecars', 'Size sidecars based on connection count, not request count.'),
            ('Mesh control plane HA', 'Always run multiple replicas of istiod / linkerd-destination.')
        ],
        'learn_more': [
            ('Istio Sidecar', 'https://istio.io/latest/docs/reference/config/networking/sidecar/'),
            ('Linkerd Architecture', 'https://linkerd.io/2/reference/architecture/')
        ],
        'takeaway': 'The sidecar is part of your blast radius. Plan for it to die — same as any other container.',
    },
    'third_party_outage': {
        'title': 'A third-party SaaS dependency went down',
        'summary': 'Payment provider became unreachable. Checkout fell back to queueing orders for later processing. Customer-facing degradation, not outage.',
        'what_happens': [
            'Calls to Stripe/Twilio/SendGrid/Auth0 start timing out.',
            'Circuit breaker around the third-party trips after consecutive failures.',
            "Requests that depend on the third-party return a degraded response: 'Payment will be processed shortly.'",
            'Successful order data is queued for replay when the third-party recovers.',
            'Status page for the third-party confirms the outage. Customers are informed.',
            'Third-party recovers. Queued operations replay in order, with deduplication keys to prevent doubles.',
            'Mitigation strategy: idempotency keys, queue-and-retry pattern, redundancy across vendors (multi-payment-provider).'
        ],
        'primitives': [
            ('Circuit Breaker (on external calls)', 'Same pattern, different blast radius.'),
            ('Idempotency Keys', "Same op done twice doesn't double-charge."),
            ('Queue-and-Replay', 'Persist work that depends on degraded third-party, replay on recovery.'),
            ('Vendor Redundancy', 'Multi-provider for payment, email, SMS — expensive but resilient.'),
            ('Graceful Degradation', 'Show users the system is degraded, not broken.')
        ],
        'learn_more': [
            ('Stripe — Designing for Reliability', 'https://stripe.com/blog/designing-for-reliability'),
            ('Idempotent APIs (Stripe)', 'https://stripe.com/docs/api/idempotent_requests')
        ],
        'takeaway': 'Every external dependency is an outage waiting to happen. Design checkout to survive Stripe going down — your business depends on it.',
    },
}

SCENARIOS = {
    'black_friday': {
        'name': 'Black Friday',
        'icon': '🛒',
        'duration_sec': 90,
        'description': 'Sustained traffic spike across all services. Watch HPA scale, latency climb, and the system absorb it.',
        'steps': [
            {
                'at': 0,
                'action': 'cpu_pressure',
                'service': 'api-gateway',
                'duration': 30,
            },
            {
                'at': 5,
                'action': 'cpu_pressure',
                'service': 'task-service',
                'duration': 35,
            },
            {
                'at': 12,
                'action': 'cpu_pressure',
                'service': 'user-service',
                'duration': 25,
            },
            {
                'at': 30,
                'action': 'kill_pod',
                'service': 'api-gateway',
            }
        ],
        'takeaway': 'A real load event is rarely one thing. CPU pressure + occasional pod losses + sustained duration is the realistic shape.',
    },
    'region_disaster': {
        'name': 'Region Disaster',
        'icon': '🗺️',
        'duration_sec': 60,
        'description': 'Full AZ outage with sustained recovery. Half the cluster vanishes, autoscaler provisions new capacity.',
        'steps': [
            {
                'at': 0,
                'action': 'region_outage',
                'duration': 30,
            },
            {
                'at': 35,
                'action': 'cpu_pressure',
                'service': 'api-gateway',
                'duration': 15,
            }
        ],
        'takeaway': 'Surviving an AZ outage requires the workload to be multi-AZ at the pod level — not just the cluster level.',
    },
    'bad_deploy_day': {
        'name': 'Bad Deploy Day',
        'icon': '💣',
        'duration_sec': 70,
        'description': 'Three deployments in a row, two of them broken. Watch RollingUpdate and rollback save the system.',
        'steps': [
            {
                'at': 0,
                'action': 'bad_deploy',
                'service': 'task-service',
            },
            {
                'at': 20,
                'action': 'bad_deploy',
                'service': 'user-service',
            },
            {
                'at': 45,
                'action': 'kill_pod',
                'service': 'notification-service',
            }
        ],
        'takeaway': 'Multiple bad deploys in a single day is the realistic case. RollingUpdate with maxUnavailable=0 means each one fails safely.',
    },
    'security_incident': {
        'name': 'Security Incident',
        'icon': '🔓',
        'duration_sec': 50,
        'description': 'Suspected breach. Rotate keys. Isolate a service. Watch the system stay up through both events.',
        'steps': [
            {
                'at': 0,
                'action': 'expire_jwt',
            },
            {
                'at': 12,
                'action': 'network_partition',
                'service': 'task-service',
                'duration': 20,
            }
        ],
        'takeaway': 'Security response is also an availability test. Key rotation + service isolation should not cause user-visible downtime.',
    },
    'cache_cascade': {
        'name': 'Cache → DB Cascade',
        'icon': '📉',
        'duration_sec': 55,
        'description': 'Redis dies, the DB gets hit harder than ever, then the DB dies too. Cascading failure mode.',
        'steps': [
            {
                'at': 0,
                'action': 'redis_failure',
                'duration': 25,
            },
            {
                'at': 10,
                'action': 'db_failure',
                'duration': 20,
            },
            {
                'at': 30,
                'action': 'cpu_pressure',
                'service': 'api-gateway',
                'duration': 15,
            }
        ],
        'takeaway': 'Caches protect databases. When the cache dies, the DB takes its full unprotected load. Plan for it.',
    },
    'friday_evening': {
        'name': 'Friday 4:55 PM Deploy',
        'icon': '🍻',
        'duration_sec': 80,
        'description': 'Someone shipped a deploy 5 minutes before the weekend. The on-call rotation is already at the bar. Watch the system save itself.',
        'steps': [
            {
                'at': 0,
                'action': 'bad_deploy',
                'service': 'task-service',
            },
            {
                'at': 18,
                'action': 'cpu_pressure',
                'service': 'user-service',
                'duration': 25,
            },
            {
                'at': 35,
                'action': 'thundering_herd',
                'duration': 18,
            }
        ],
        'takeaway': "Auto-rollback exists precisely so on-call doesn't have to be at the laptop. The system should save itself when the team can't.",
    },
    'christmas_eve': {
        'name': 'Christmas Eve',
        'icon': '🎄',
        'duration_sec': 100,
        'description': 'Peak holiday traffic. Half the team is on PTO. The senior SRE is offline. Everything that could break, breaks.',
        'steps': [
            {
                'at': 0,
                'action': 'cpu_pressure',
                'service': 'api-gateway',
                'duration': 35,
            },
            {
                'at': 12,
                'action': 'redis_failure',
                'duration': 28,
            },
            {
                'at': 25,
                'action': 'kill_pod',
                'service': 'task-service',
            },
            {
                'at': 45,
                'action': 'third_party_outage',
                'duration': 25,
            }
        ],
        'takeaway': "The systems you build are tested by the moments when humans can't intervene. Resilience is paid for in advance.",
    },
    'the_audit': {
        'name': 'The Audit',
        'icon': '📋',
        'duration_sec': 75,
        'description': 'External auditors are pulling evidence during normal operations. Chaos must continue without affecting compliance posture.',
        'steps': [
            {
                'at': 0,
                'action': 'expire_jwt',
            },
            {
                'at': 15,
                'action': 'secret_leak',
                'duration': 20,
            },
            {
                'at': 35,
                'action': 'cert_expiry',
            },
            {
                'at': 55,
                'action': 'kill_pod',
                'service': 'user-service',
            }
        ],
        'takeaway': "Compliance is an availability property, not a separate axis. The audit doesn't pause your incidents.",
    },
    'active_breach': {
        'name': 'Active Breach',
        'icon': '🚨',
        'duration_sec': 70,
        'description': "Security signals indicate intrusion. Segment compromised services. Rotate credentials. Don't take the whole system down.",
        'steps': [
            {
                'at': 0,
                'action': 'secret_leak',
                'duration': 25,
            },
            {
                'at': 10,
                'action': 'network_partition',
                'service': 'task-service',
                'duration': 25,
            },
            {
                'at': 30,
                'action': 'expire_jwt',
            },
            {
                'at': 45,
                'action': 'cascading_failure',
            }
        ],
        'takeaway': 'Incident response is also an availability test. Containment without total shutdown is the goal.',
    },
    'upgrade_gone_wrong': {
        'name': 'Upgrade Gone Wrong',
        'icon': '⚠️',
        'duration_sec': 90,
        'description': 'A Kubernetes minor-version upgrade introduced subtle scheduling regressions. The cluster is under pressure during the migration.',
        'steps': [
            {
                'at': 0,
                'action': 'api_throttle',
                'duration': 25,
            },
            {
                'at': 20,
                'action': 'autoscaler_stuck',
                'duration': 22,
            },
            {
                'at': 40,
                'action': 'noisy_neighbor',
                'duration': 28,
            },
            {
                'at': 65,
                'action': 'kill_pod',
                'service': 'api-gateway',
            }
        ],
        'takeaway': 'Cluster upgrades are deployments too. Test them in staging, watch them in production, and have rollback paths.',
    },
}
