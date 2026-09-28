import json

class Process:
    def __init__(self, pid, total_data_size):
        self.pid = pid
        self.total_data_size = total_data_size
        self.data_in_vram = 0
        self.work_completed = 0

    def load_data(self, amount):
        # Can't load more than total data
        to_load = min(amount, self.total_data_size - self.data_in_vram - self.work_completed)
        self.data_in_vram += to_load
        return to_load

    def compute(self, amount):
        # Can't compute more than what is in VRAM
        to_compute = min(amount, self.data_in_vram)
        self.work_completed += to_compute
        self.data_in_vram -= to_compute
        return to_compute

class GPUSimulator:
    def __init__(self, mode, total_time):
        self.mode = mode
        self.total_time = total_time
        
        # GPU Characteristics
        self.vram_bandwidth = 150 # units of data loaded per time step
        self.compute_rate = 100 # units of data processed per time step
        self.context_switch_penalty = 10 # time steps lost during a context switch
        
        # Modes
        if self.mode == 'Hardware Arbitration':
            self.time_slice = 5 # rapid context switching
        else:
            self.time_slice = 100 # Kairos software scheduling (longer slices)
            
        self.processes = [Process(i, 10000) for i in range(4)]
        
        # Metrics
        self.total_data_loaded = 0
        self.total_compute_done = 0
        self.context_switches = 0
        self.time_wasted_on_switches = 0

    def run(self):
        time = 0
        active_pid = 0
        slice_timer = 0
        
        while time < self.total_time:
            # Check if all processes are done
            if all(p.work_completed >= p.total_data_size for p in self.processes):
                break
                
            p = self.processes[active_pid]
            
            # Skip if this process is done
            if p.work_completed >= p.total_data_size:
                active_pid = (active_pid + 1) % len(self.processes)
                continue
                
            if slice_timer >= self.time_slice:
                # Context switch
                self.context_switches += 1
                self.time_wasted_on_switches += self.context_switch_penalty
                time += self.context_switch_penalty
                
                # When switching, VRAM is heavily contested/cleared by the next process
                # We simulate this by dropping the uncomputed data from VRAM
                p.data_in_vram = 0 
                
                active_pid = (active_pid + 1) % len(self.processes)
                slice_timer = 0
                continue
                
            # Execute step
            # 1. Load data to VRAM
            loaded = p.load_data(self.vram_bandwidth)
            self.total_data_loaded += loaded
            
            # 2. Compute
            computed = p.compute(self.compute_rate)
            self.total_compute_done += computed
            
            time += 1
            slice_timer += 1
            
        # Calculate final metrics
        actual_time = time if time > 0 else 1
        data_utilization = (self.total_compute_done / self.total_data_loaded) * 100 if self.total_data_loaded > 0 else 0
        switch_overhead = (self.time_wasted_on_switches / actual_time) * 100
        throughput = self.total_compute_done / actual_time
        
        return {
            "Mode": self.mode,
            "Data Utilization Percentage": f"{data_utilization:.2f}%",
            "Context Switch Overhead": f"{switch_overhead:.2f}%",
            "Overall Throughput (units/step)": f"{throughput:.2f}",
            "Total Compute Done": self.total_compute_done,
            "Total Data Transferred": self.total_data_loaded,
            "Number of Context Switches": self.context_switches,
            "Completion Time": actual_time
        }

if __name__ == "__main__":
    SIM_TIME = 2000
    
    sim_hw = GPUSimulator('Hardware Arbitration', SIM_TIME)
    res_hw = sim_hw.run()
    
    sim_kairos = GPUSimulator('Kairos Software Scheduling', SIM_TIME)
    res_kairos = sim_kairos.run()
    
    print("==================================================")
    print("   GPU SCHEDULING & LOCALITY SIMULATION RESULTS   ")
    print("==================================================")
    print(f"Total Simulation Time Steps: {SIM_TIME}")
    print("\n[Scenario 1] Hardware-Based Arbitration")
    print("Description: Rapid context switching responding instantly to competition.")
    for k, v in res_hw.items():
        if k != "Mode":
            print(f" - {k}: {v}")
            
    print("\n[Scenario 2] Kairos Software Scheduling")
    print("Description: Longer time slices, prioritizing data locality and utilization.")
    for k, v in res_kairos.items():
        if k != "Mode":
            print(f" - {k}: {v}")
    print("==================================================")
