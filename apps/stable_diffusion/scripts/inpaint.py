import torch
import time
from PIL import Image
import transformers
from apps.stable_diffusion.src import (
    args,
    InpaintPipeline,
    get_schedulers,
    set_init_device_flags,
    utils,
    clear_all,
    save_output_img,
)
from apps.stable_diffusion.src.utils import get_generation_text_info


# set initial values of iree_vulkan_target_triple, use_tuned and import_mlir.
init_iree_vulkan_target_triple = args.iree_vulkan_target_triple
init_use_tuned = args.use_tuned
init_import_mlir = args.import_mlir


# Exposed to UI.
def inpaint_inf(
    prompt: str,
    negative_prompt: str,
    image_dict,
    height: int,
    width: int,
    inpaint_full_res: bool,
    inpaint_full_res_padding: int,
    steps: int,
    guidance_scale: float,
    seed: int,
    batch_count: int,
    batch_size: int,
    scheduler: str,
    custom_model: str,
    hf_model_id: str,
    precision: str,
    device: str,
    max_length: int,
    save_metadata_to_json: bool,
    save_metadata_to_png: bool,
    lora_weights: str,
    lora_hf_id: str,
    ondemand: bool,
):
    from apps.stable_diffusion.web.ui.utils import (
        get_custom_model_pathfile,
        get_custom_vae_or_lora_weights,
        Config,
    )
    import apps.stable_diffusion.web.utils.global_obj as global_obj
    import apps.stable_diffusion.src.utils.state_manager as state_manager

    if not state_manager.app.is_ready():
        return

    args.prompts = [prompt]
    args.negative_prompts = [negative_prompt]
    args.guidance_scale = guidance_scale
    args.steps = steps
    args.scheduler = scheduler
    args.img_path = "not none"
    args.mask_path = "not none"
    args.ondemand = ondemand
    if ondemand and batch_count > 1:
        print("Low VRAM mode currently only supports 1 batch count.")
        batch_count = 1

    # set ckpt_loc and hf_model_id.
    args.ckpt_loc = ""
    args.hf_model_id = ""
    if custom_model == "None":
        if not hf_model_id:
            return (
                None,
                "Please provide either custom model or huggingface model ID, both must not be empty",
            )
        args.hf_model_id = hf_model_id
    elif ".ckpt" in custom_model or ".safetensors" in custom_model:
        args.ckpt_loc = get_custom_model_pathfile(custom_model)
    else:
        args.hf_model_id = custom_model

    args.use_lora = get_custom_vae_or_lora_weights(
        lora_weights, lora_hf_id, "lora"
    )

    # TODO: StateManager Try starts here
    # try:
    state_manager.app.set_job(f"Initializing model {custom_model}", False)
    args.save_metadata_to_json = save_metadata_to_json
    args.write_metadata_to_png = save_metadata_to_png

    dtype = torch.float32 if precision == "fp32" else torch.half
    cpu_scheduling = not scheduler.startswith("Shark")
    new_config_obj = Config(
        "inpaint",
        args.hf_model_id,
        args.ckpt_loc,
        precision,
        batch_size,
        max_length,
        height,
        width,
        device,
        use_lora=args.use_lora,
        use_stencil=None,
        ondemand=ondemand,
    )
    if (
        args.ondemand
        or not global_obj.get_sd_obj()
        or global_obj.get_cfg_obj() != new_config_obj
    ):
        global_obj.clear_cache()
        global_obj.set_cfg_obj(new_config_obj)
        args.precision = precision
        args.batch_count = batch_count
        args.batch_size = batch_size
        args.max_length = max_length
        args.height = height
        args.width = width
        args.device = device.split("=>", 1)[1].strip()
        args.iree_vulkan_target_triple = init_iree_vulkan_target_triple
        args.use_tuned = init_use_tuned
        args.import_mlir = init_import_mlir
        set_init_device_flags()
        model_id = (
            args.hf_model_id
            if args.hf_model_id
            else "stabilityai/stable-diffusion-2-inpainting"
        )
        global_obj.set_schedulers(get_schedulers(model_id))
        scheduler_obj = global_obj.get_scheduler(scheduler)
        global_obj.set_sd_obj(
            InpaintPipeline.from_pretrained(
                scheduler=scheduler_obj,
                import_mlir=args.import_mlir,
                model_id=args.hf_model_id,
                ckpt_loc=args.ckpt_loc,
                custom_vae=args.custom_vae,
                precision=args.precision,
                max_length=args.max_length,
                batch_size=args.batch_size,
                height=args.height,
                width=args.width,
                use_base_vae=args.use_base_vae,
                use_tuned=args.use_tuned,
                low_cpu_mem_usage=args.low_cpu_mem_usage,
                debug=args.import_debug if args.import_mlir else False,
                use_lora=args.use_lora,
                ondemand=args.ondemand,
            )
        )

    global_obj.set_sd_scheduler(scheduler)

    start_time = time.time()
    global_obj.get_sd_obj().log = ""
    generated_imgs = []
    seeds = []
    img_seed = utils.sanitize_seed(seed)
    image = image_dict["image"]
    mask_image = image_dict["mask"]
    text_output = ""
    for i in range(batch_count):
        state_manager.app.set_job(
            "Running inpaint job", False, i, batch_count, steps
        )
        if i > 0:
            img_seed = utils.sanitize_seed(-1)
        out_imgs = global_obj.get_sd_obj().generate_images(
            prompt,
            negative_prompt,
            image,
            mask_image,
            batch_size,
            height,
            width,
            inpaint_full_res,
            inpaint_full_res_padding,
            steps,
            guidance_scale,
            img_seed,
            args.max_length,
            dtype,
            args.use_base_vae,
            cpu_scheduling,
        )
        seeds.append(img_seed)
        total_time = time.time() - start_time
        text_output = get_generation_text_info(seeds, device)
        text_output += "\n" + global_obj.get_sd_obj().log
        text_output += f"\nTotal image(s) generation time: {total_time:.4f}sec"

        if state_manager.app.is_canceling():
            break
        else:
            save_output_img(out_imgs[0], img_seed)
            generated_imgs.extend(out_imgs)
            yield generated_imgs, text_output

    # TODO: StateManager Try ends here
    # except Exception:
    #     state_manager.app.set_ready()
    #     raise

    state_manager.app.set_ready()
    return generated_imgs, text_output


def main():
    if args.clear_all:
        clear_all()

    if args.img_path is None:
        print("Flag --img_path is required.")
        exit()
    if args.mask_path is None:
        print("Flag --mask_path is required.")
        exit()

    dtype = torch.float32 if args.precision == "fp32" else torch.half
    cpu_scheduling = not args.scheduler.startswith("Shark")
    set_init_device_flags()
    model_id = (
        args.hf_model_id
        if "inpaint" in args.hf_model_id
        else "stabilityai/stable-diffusion-2-inpainting"
    )
    schedulers = get_schedulers(model_id)
    scheduler_obj = schedulers[args.scheduler]
    seed = args.seed
    image = Image.open(args.img_path)
    mask_image = Image.open(args.mask_path)

    inpaint_obj = InpaintPipeline.from_pretrained(
        scheduler=scheduler_obj,
        import_mlir=args.import_mlir,
        model_id=args.hf_model_id,
        ckpt_loc=args.ckpt_loc,
        custom_vae=args.custom_vae,
        precision=args.precision,
        max_length=args.max_length,
        batch_size=args.batch_size,
        height=args.height,
        width=args.width,
        use_base_vae=args.use_base_vae,
        use_tuned=args.use_tuned,
        low_cpu_mem_usage=args.low_cpu_mem_usage,
        debug=args.import_debug if args.import_mlir else False,
        use_lora=args.use_lora,
        ondemand=args.ondemand,
    )

    for current_batch in range(args.batch_count):
        if current_batch > 0:
            seed = -1
        seed = utils.sanitize_seed(seed)

        start_time = time.time()
        generated_imgs = inpaint_obj.generate_images(
            args.prompts,
            args.negative_prompts,
            image,
            mask_image,
            args.batch_size,
            args.height,
            args.width,
            args.inpaint_full_res,
            args.inpaint_full_res_padding,
            args.steps,
            args.guidance_scale,
            seed,
            args.max_length,
            dtype,
            args.use_base_vae,
            cpu_scheduling,
        )
        total_time = time.time() - start_time
        text_output = f"prompt={args.prompts}"
        text_output += f"\nnegative prompt={args.negative_prompts}"
        text_output += (
            f"\nmodel_id={args.hf_model_id}, ckpt_loc={args.ckpt_loc}"
        )
        text_output += f"\nscheduler={args.scheduler}, device={args.device}"
        text_output += f"\nsteps={args.steps}, guidance_scale={args.guidance_scale}, seed={seed}, size={args.height}x{args.width}"
        text_output += (
            f", batch size={args.batch_size}, max_length={args.max_length}"
        )
        text_output += inpaint_obj.log
        text_output += f"\nTotal image generation time: {total_time:.4f}sec"

        save_output_img(generated_imgs[0], seed)
        print(text_output)


if __name__ == "__main__":
    main()
