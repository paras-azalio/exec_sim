package ilink.nei.clicr.any;

import cliautomation.api.CliAutomationEngine;
import cliautomation.exec.ExecutionResult;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Standalone driver that runs a CLI-CR workflow against the local node
 * simulator, without the ilink macro-server / GRC configuration.
 *
 * <p>In production, {@link MopExecutionUtil} resolves NIAM_IP / M2MPORT /
 * REPO_IP from the GRC section and puts them into the engine's selector map.
 * That is the only thing this class replaces: it puts localhost + the
 * simulator's port into the very same selector keys, so the workflow YAML is
 * consumed completely unmodified and every {@code when}, {@code register} and
 * {@code validation.criteria} in it is evaluated for real.
 *
 * <p>The execution-report JSON is produced by the same
 * {@code MopExecutionUtil.buildExecutionReport} used in production, so the
 * output is directly comparable with a real node run.
 *
 * <pre>
 * java -Dclicr.local.shell="C:\Program Files\Git\bin\bash.exe" \
 *      -cp "target/classes;target/lib/*" ilink.nei.clicr.any.SimRunner \
 *      --template  .../DPA_DLU_INSTALLATION.yaml \
 *      --json      .../DPA_DLU_INSTALLATION_East.json \
 *      --schema    .../DPA_DLU_INSTALLATION_validation-rules.yaml \
 *      --node East --cr CR1 --node-type DPA --sub-activity DLU_INSTALLATION \
 *      --out .../out/success --sim-host 127.0.0.1 --sim-port 2222 \
 *      --rollback-only false
 * </pre>
 */
public final class SimRunner {

    public static void main(String[] args) throws Exception {
        Map<String, String> a = parseArgs(args);

        String template = req(a, "template");
        String jsonFile = req(a, "json");
        String schemaFile = req(a, "schema");
        String nodeName = a.getOrDefault("node", "East");
        String crName = a.getOrDefault("cr", "CR1");
        String nodeType = a.getOrDefault("node-type", "DPA");
        String subActivity = a.getOrDefault("sub-activity", "DLU_INSTALLATION");
        String outDir = a.getOrDefault("out", "out");
        String simHost = a.getOrDefault("sim-host", "127.0.0.1");
        String simPort = a.getOrDefault("sim-port", "2222");
        String repoPort = a.getOrDefault("repo-port", simPort);
        boolean rollbackOnly = "true".equalsIgnoreCase(a.getOrDefault("rollback-only", "false"));

        Files.createDirectories(Paths.get(outDir));

        HashMap<String, Object> selector = new HashMap<String, Object>();
        selector.put("nodeName", nodeName);
        selector.put("node", nodeName);
        selector.put("CR_NAME", crName);
        selector.put("currentCrGroup", crName);

        // ---- the ONLY substitution vs. production: point the node/repo
        // ---- endpoints at the simulator instead of the real network.
        selector.put("NIAM_IP", simHost);
        selector.put("M2MPORT", simPort);
        selector.put("M2MUSER", a.getOrDefault("m2m-user", "g_MANO_MAVMS_DM"));
        selector.put("M2MPASSWORD", a.getOrDefault("m2m-password", "simulated"));
        selector.put("REPO_IP", simHost);
        selector.put("REPO_USER", a.getOrDefault("repo-user", "installer"));
        selector.put("REPO_PASSWORD", a.getOrDefault("repo-password", "simulated"));
        selector.put("REPO_PORT", repoPort);

        // workflow inputs normally supplied via the Arglist
        selector.put("ORDER_NO", a.getOrDefault("order-no", "CR-DPA_DLU05082026"));
        selector.put("PARENT_REQ_ID", a.getOrDefault("parent-req-id", "40501"));
        selector.put("ALERT_EMAIL_TO", a.getOrDefault("email-to", "paras@example.com"));
        selector.put("ALERT_EMAIL_CC", a.getOrDefault("email-cc", ""));
        selector.put("GITLAB_CLIENT_ID", "sim");
        selector.put("GITLAB_CLIENT_SECRET", "sim");
        selector.put("GITLAB_PROJECT_ID", "sim");

        if (rollbackOnly) {
            selector.put("ROLLBACK_ONLY", "true");
            selector.put("ROLLBACK_REQUIRED", "true");
        }

        // --vars "K=V;K2=V2" - anything else the Arglist would normally carry
        // (CHILD_REQ_ID, LICENSE_FILE_PATH, INPUT_JSON_FILE_NAME, ...). Applied
        // last so a caller can also override any default set above.
        String extraVars = a.get("vars");
        if (extraVars != null && !extraVars.trim().isEmpty()) {
            for (String pair : extraVars.split(";")) {
                int eq = pair.indexOf('=');
                if (eq > 0) {
                    selector.put(pair.substring(0, eq).trim(), pair.substring(eq + 1).trim());
                }
            }
        }

        System.out.println("=== SimRunner ===");
        System.out.println("  template      : " + template);
        System.out.println("  ciq json      : " + jsonFile);
        System.out.println("  node / cr     : " + nodeName + " / " + crName);
        System.out.println("  simulator     : " + simHost + ":" + simPort);
        System.out.println("  rollback only : " + rollbackOnly);
        System.out.println("  output dir    : " + outDir);
        System.out.println("=================");

        long t0 = System.currentTimeMillis();
        CliAutomationEngine engine = new CliAutomationEngine();
        ExecutionResult result = engine.executeForNode(template, selector, jsonFile, schemaFile);
        long took = System.currentTimeMillis() - t0;

        String nodeDetails = MopExecutionUtil.resolveNodeDetails(jsonFile, nodeName);
        String nodeDisplayName = (nodeDetails != null && !nodeDetails.trim().isEmpty())
                ? nodeName + " (" + nodeDetails.trim() + ")"
                : nodeName;

        Map<String, Object> report = MopExecutionUtil.buildExecutionReport(nodeDisplayName, result);
        String path = MopExecutionUtil.writeExecutionReportFile(outDir, nodeType, subActivity,
                nodeName, report);

        // final variable snapshot - handy when checking which ROLLBACK_REQUIRED
        // transition the workflow actually took.
        Map<String, Object> vars = new LinkedHashMap<String, Object>();
        if (result.getFinalVariables() != null) {
            vars.putAll(result.getFinalVariables());
        }
        Path varsPath = Paths.get(outDir, nodeType + "_" + subActivity + "_VARIABLES_"
                + nodeName + ".json");
        Files.write(varsPath, MopExecutionUtil.toJson(vars).getBytes(StandardCharsets.UTF_8));

        System.out.println();
        System.out.println("overall success : " + result.isSuccess());
        System.out.println("took            : " + (took / 1000) + "s");
        System.out.println("executionReport : " + path);
        System.out.println("variables       : " + varsPath.toAbsolutePath());
        System.out.println("ROLLBACK_REQUIRED = " + vars.get("ROLLBACK_REQUIRED"));

        System.exit(result.isSuccess() ? 0 : 1);
    }

    private static Map<String, String> parseArgs(String[] args) {
        Map<String, String> m = new LinkedHashMap<String, String>();
        for (int i = 0; i < args.length; i++) {
            String k = args[i];
            if (!k.startsWith("--")) {
                continue;
            }
            k = k.substring(2);
            String v = (i + 1 < args.length && !args[i + 1].startsWith("--")) ? args[++i] : "true";
            m.put(k, v);
        }
        return m;
    }

    private static String req(Map<String, String> a, String key) {
        String v = a.get(key);
        if (v == null || v.trim().isEmpty()) {
            throw new IllegalArgumentException("missing required --" + key);
        }
        if (!new File(v).exists()) {
            throw new IllegalArgumentException("--" + key + " not found: " + v);
        }
        return v;
    }

    private SimRunner() {
    }
}
