from fastapi import APIRouter
from app.services.listmonk_client import listmonk

router = APIRouter()


@router.get("")
async def get_templates():
    return await listmonk.get_templates()


@router.get("/{template_id}")
async def get_template(template_id: int):
    return await listmonk.get_template(template_id)


@router.post("")
async def create_template(data: dict):
    return await listmonk.create_template(data)


@router.put("/{template_id}")
async def update_template(template_id: int, data: dict):
    return await listmonk.update_template(template_id, data)


@router.put("/{template_id}/default")
async def set_default_template(template_id: int):
    return await listmonk.set_default_template(template_id)


@router.delete("/{template_id}")
async def delete_template(template_id: int):
    return await listmonk.delete_template(template_id)
