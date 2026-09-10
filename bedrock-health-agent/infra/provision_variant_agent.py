#!/usr/bin/env python3
"""
Provision a second Bedrock Agent + alias that reuses the existing SimpleCover demo's
IAM role, Lambda tools function, and Knowledge Base, but points at a
different foundation model. Built for running a Datadog LLM-Obs experiment
that compares model versions on the same tools/KB.

Does NOT touch config.STATE_FILE (.deployment.json) — writes to
.deployment.variant.json instead, so the primary deployment is untouched.

Run:  python infra/provision_variant_agent.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTRUCTIONS_PATH = os.path.join(REPO, "agent", "instructions.txt")
FUNCTIONS_PATH = os.path.join(REPO, "agent", "action_group_functions.json")

VARIANT_MODEL_ID = os.environ.get(
    "SIMPLE_COVER_VARIANT_MODEL_ID", "apac.anthropic.claude-sonnet-4-20250514-v1:0"
)
VARIANT_AGENT_NAME = f"{config.PREFIX}-agent-sonnet4"
VARIANT_ALIAS_NAME = "demo-sonnet4"
VARIANT_STATE_FILE = os.path.join(REPO, ".deployment.variant.json")


def load_variant_state():
    if os.path.exists(VARIANT_STATE_FILE):
        with open(VARIANT_STATE_FILE) as f:
            return json.load(f)
    return {}


def save_variant_state(state):
    with open(VARIANT_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"  variant state -> {VARIANT_STATE_FILE}")


def main():
    c = common.clients()
    ba = c["bedrock_agent"]

    base_state = common.load_state()
    for key in ("agent_role_arn", "lambda_arn", "lambda_name", "kb_id"):
        if not base_state.get(key):
            raise RuntimeError(
                f"Base deployment state missing '{key}'. "
                "Run infra/provision.py first to stand up the primary agent."
            )

    state = load_variant_state()
    state.update({
        "region": config.REGION,
        "base_agent_id": base_state["agent_id"],
        "model_id": VARIANT_MODEL_ID,
    })
    save_variant_state(state)

    with open(INSTRUCTIONS_PATH) as f:
        instructions = f.read()
    with open(FUNCTIONS_PATH) as f:
        functions = json.load(f)["functions"]

    # --- Agent -----------------------------------------------------------
    if not state.get("agent_id"):
        r = ba.create_agent(
            agentName=VARIANT_AGENT_NAME,
            agentResourceRoleArn=base_state["agent_role_arn"],
            foundationModel=VARIANT_MODEL_ID,
            instruction=instructions,
            idleSessionTTLInSeconds=1800,
        )
        state["agent_id"] = r["agent"]["agentId"]
        save_variant_state(state)
        common.log(f"  created variant agent {state['agent_id']} ({VARIANT_MODEL_ID})")

    agent_id = state["agent_id"]
    acct = common.account_id(c)
    agent_arn = f"arn:aws:bedrock:{config.REGION}:{acct}:agent/{agent_id}"
    state["agent_arn"] = agent_arn

    def _agent_status(want):
        def _f():
            s = ba.get_agent(agentId=agent_id)["agent"]["agentStatus"]
            return (s in want, s)
        return _f

    common.wait_until(_agent_status({"NOT_PREPARED", "PREPARED"}),
                      "variant agent ready for edits", timeout=300, interval=10)

    # Lambda resource policy for THIS agent's arn (separate statement id).
    try:
        c["lambda"].add_permission(
            FunctionName=base_state["lambda_name"],
            StatementId="bedrock-agent-invoke-variant-sonnet4",
            Action="lambda:InvokeFunction",
            Principal="bedrock.amazonaws.com",
            SourceArn=agent_arn,
        )
        common.log("  added Lambda invoke permission for variant agent")
    except c["lambda"].exceptions.ResourceConflictException:
        common.log("  Lambda invoke permission already present for variant agent")

    # --- Action group (reuse existing Lambda) -----------------------------
    if not state.get("action_group_id"):
        ag = ba.create_agent_action_group(
            agentId=agent_id, agentVersion="DRAFT",
            actionGroupName=config.ACTION_GROUP,
            actionGroupExecutor={"lambda": base_state["lambda_arn"]},
            functionSchema={"functions": functions},
            actionGroupState="ENABLED",
        )
        state["action_group_id"] = ag["agentActionGroup"]["actionGroupId"]
        save_variant_state(state)
        common.log(f"  created variant action group {state['action_group_id']}")

    # --- KB association (reuse existing KB) -------------------------------
    if not state.get("kb_associated"):
        try:
            ba.associate_agent_knowledge_base(
                agentId=agent_id, agentVersion="DRAFT",
                knowledgeBaseId=base_state["kb_id"],
                description=("Product tiers, premiums, waiting periods, and general "
                             "info on rebate/MLS/LHC. Search this for any product or "
                             "how-it-works question."),
                knowledgeBaseState="ENABLED",
            )
            state["kb_associated"] = True
            save_variant_state(state)
            common.log("  associated existing knowledge base with variant agent")
        except Exception as e:  # noqa: BLE001
            if "already" not in str(e).lower():
                raise

    # --- Prepare + alias ---------------------------------------------------
    ba.prepare_agent(agentId=agent_id)
    common.wait_until(_agent_status({"PREPARED"}), "variant agent prepared",
                      timeout=300, interval=10)

    if not state.get("agent_alias_id"):
        al = ba.create_agent_alias(agentId=agent_id, agentAliasName=VARIANT_ALIAS_NAME)
        state["agent_alias_id"] = al["agentAlias"]["agentAliasId"]
        save_variant_state(state)
        common.log(f"  created variant alias {state['agent_alias_id']}")

    def _alias_ready():
        s = ba.get_agent_alias(agentId=agent_id,
                               agentAliasId=state["agent_alias_id"]
                               )["agentAlias"]["agentAliasStatus"]
        return (s == "PREPARED", s)

    common.wait_until(_alias_ready, "variant alias prepared", timeout=300, interval=10)

    save_variant_state(state)
    common.log("DONE. Variant agent ready.")
    common.log(f"  model        = {VARIANT_MODEL_ID}")
    common.log(f"  agentId      = {state['agent_id']}")
    common.log(f"  agentAliasId = {state['agent_alias_id']}")


if __name__ == "__main__":
    main()
