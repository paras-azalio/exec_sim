import cliautomation.api.CliAutomationEngine;
import cliautomation.exec.ExecutionListener;
import cliautomation.exec.ExecutionResult;
import cliautomation.exec.StepExecutionResult;

import java.util.HashMap;

/**
 * Minimal driver: runs the CLI-CR engine in EXECUTE mode against a workflow,
 * printing each step as it runs. Args: <yaml> <nodeName> <ciq.json> <schema.yaml>
 */
public class RunExec {
    public static void main(String[] args) throws Exception {
        final String yaml = args[0];
        final String nodeName = args[1];
        final String json = args[2];
        final String schema = args[3];

        CliAutomationEngine engine = new CliAutomationEngine();
        HashMap<String, Object> selector = new HashMap<String, Object>();
        selector.put("nodeName", nodeName);
        selector.put("runMode", "EXECUTE");

        ExecutionListener listener = new ExecutionListener() {
            public void onStepStart(String stepId, String node, String phase, String command) {
                System.out.println();
                System.out.println(">> [" + phase + "] " + node + " : " + command);
            }
            public void onStepResult(StepExecutionResult r) {
                String out = r.getOutput() == null ? "" : r.getOutput().replaceAll("\\s+", " ").trim();
                if (out.length() > 140) out = out.substring(0, 140) + " ...";
                System.out.println("   -> success=" + r.isSuccess()
                        + " exit=" + r.getExitCode()
                        + " validated=" + r.isValidationCriteriaMatched()
                        + " warn=" + r.isValidationWarning());
                if (r.getMessage() != null && !r.getMessage().isEmpty())
                    System.out.println("      msg: " + r.getMessage());
                System.out.println("      out: " + out);
            }
        };

        System.out.println("=== Running workflow " + yaml + " for node " + nodeName + " ===");
        ExecutionResult res = engine.executeForNode(yaml, selector, json, schema, null, listener);
        System.out.println();
        System.out.println("==================== OVERALL success=" + res.isSuccess() + " ====================");
        System.out.println("final vars (subset): SIGNALINGBASE.len="
                + String.valueOf(res.getFinalVariables().get("SIGNALINGBASE")).length());
    }
}
