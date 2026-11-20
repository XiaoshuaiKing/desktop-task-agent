from pathlib import Path
import logging

logger=logging.getLogger(__name__)

SKILLS_DIR=Path(__file__).resolve().parent.parent/"skills"

# 解析 SKILL.md 的元数据
def parse_skill_metadata(skill_file):
    text=skill_file.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None

    parts=text.split("---",2)

    if len(parts)<3:
        return None

    metadata_text=parts[1]
    metadata={}

    for line in metadata_text.strip().splitlines():
        if ":" not in line:
            continue

        key,value=line.split(":",1)
        metadata[key.strip()]=value.strip()

    if "name" not in metadata or "description" not in metadata:
        return None
    return {
        "name":metadata["name"],
        "description":metadata["description"],
        "path":skill_file
    }

# 扫描 SKILLS_DIR 下的所有技能目录
def load_skills():
    skills=[]

    if not SKILLS_DIR.exists():
        return skills

    for skill_dir in SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_file=skill_dir/"SKILL.md"
        if not skill_file.exists():
            continue
        metadata=parse_skill_metadata(skill_file)

        if metadata:
            skills.append(metadata)
    return skills

# 把所有可用技能拼成一段提示词，通常会给大模型看
def get_skills_prompt():
    skills=load_skills()
    if not skills:
        return "当前没有可用Skill。"
    lines=["当前可用Skills："]
    for skill in skills:
        lines.append(
            f"- {skill['name']}: {skill['description']}"
        )

    return "\n".join(lines)

# 根据技能名称加载完整的 SKILL.md 内容
def load_skill(skill_name):
    skills=load_skills()
    for skill in skills:
        if skill["name"]==skill_name:
            return skill["path"].read_text(encoding="utf-8")

    return None

# 加载某个技能下的参考文档
def load_reference(skill_name,reference_name):
    reference_path=SKILLS_DIR/skill_name/"references"/reference_name
    if not reference_path.exists():
        return None

    return reference_path.read_text(encoding="utf-8")

# 让模型选择skill功能
def build_skill_router_prompt(user_input):
    skills=load_skills()
    if not skills:
        return None
    skills_text="\n".join(
        f"- {skill['name']}: {skill['description']}"
        for skill in skills
    )
    prompt=f"""
    你是Skill路由器。
    根据用户输入，从下面可用Skill中选择最匹配的一个。
    如果没有任何Skill适合，返回none。
    可用Skills：
    {skills_text}
    用户输入：
    {user_input}
    只允许返回：
    - Skill名称
    - none
    不要解释，不要输出其他内容。
    """
    return prompt

# 根据用户输入选择Skill
def select_skill(model_type,user_input,call_model):
    prompt=build_skill_router_prompt(user_input)
    if prompt is None:
        return None
    messages=[
        {
            "role":"system",
            "content":"你只负责选择最匹配的Skill，不执行用户任务。"
        },
        {
            "role":"user",
            "content":prompt
        }
    ]
    message=call_model(model_type,messages,30)
    skill_name=(message.get("content") or "").strip()

    if skill_name=="none":
        return None

    skills=load_skills()
    valid_names=[skill["name"] for skill in skills]
    if skill_name not in valid_names:
        return None
    logger.info(f"调用Skill: {skill_name}")
    return skill_name