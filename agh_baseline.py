import os, random
import math
import copy
import time
import torch
import pickle
import argparse
import pprint as pp
import numpy as np
from tqdm import tqdm
import torch.nn.functional as F
from utils import move_to, load_problem
from torch.utils.data import DataLoader
from multiprocessing import Pool


def cws(input_, problem, opt):
    """
        Clarke and Wright Savings (CWS)
    """
    NODE_SIZE, SPEED = 92, 110.0

    sequences = []
    state = problem.make_state(input_)
    batch_size = input_['loc'].size(0)
    ids = torch.arange(batch_size)[:, None]

    # calculate savings
    from_depot, to_depot = input_['loc'], NODE_SIZE * input_['loc']
    from_depot, to_depot = input_['distance'].gather(1, from_depot), input_['distance'].gather(1, to_depot)
    from_depot = from_depot[:, None, :].repeat(1, opt.graph_size, 1)
    to_depot = to_depot[:, :, None].repeat(1, 1, opt.graph_size)

    i_index = input_['loc'][:, :, None].repeat(1, 1, opt.graph_size)
    j_index = input_['loc'][:, None, :].repeat(1, opt.graph_size, 1)
    i_j = NODE_SIZE * i_index + j_index
    temp_distance = input_['distance'][:, None, :].expand(batch_size, opt.graph_size, NODE_SIZE * NODE_SIZE)
    i_j_distance = temp_distance.gather(2, i_j)

    savings_distance = from_depot + to_depot - i_j_distance

    tw_left, tw_right = input_['tw_left'][:, 1:], input_['tw_right'][:, 1:]
    savings_time = tw_left[:, None, :].repeat(1, opt.graph_size, 1) - tw_left[:, :, None].repeat(1, 1, opt.graph_size)
    savings_time = savings_time - input_['duration'][:, :, None].repeat(1, 1, opt.graph_size) - i_j_distance / SPEED

    savings = savings_distance - 0.03 * 60 * savings_time

    # select first nodes
    _, selected = tw_right.sort(dim=1)
    selected = 1 + selected[:, 0]

    state = state.update(selected)

    sequences.append(selected)

    # schedule following nodes
    i = 0
    while not (state.all_finished()):
        mask = state.get_mask()
        prev = state.prev_a - 1

        score = savings[ids, prev, :][:, 0, :]
        depot_score, _ = score.min(dim=1)
        depot_score = depot_score[:, None] - 1
        score = torch.cat((depot_score, score), dim=1)
        score[mask[:, 0, :]] = -math.inf

        _, selected = score.sort(descending=True)
        selected = selected[:, 0]

        state = state.update(selected)
        sequences.append(selected)
        i += 1

    cost, _ = problem.get_costs(input_, torch.stack(sequences, 1))

    return cost, state.serve_time


def nearest_neighbor(input, problem, return_state=False):
    state = problem.make_state(input)
    sequences = []
    while not state.all_finished():
        mask = state.get_mask()
        mask = mask[:, 0, :]
        batch_size, n_loc = mask.size()
        prev_a = state.coords[state.ids, state.prev_a]  # [batch_size, 1]
        distance_index = state.NODE_SIZE * prev_a.expand(batch_size, n_loc) + state.coords  # [batch_size, n_loc]
        distance = torch.gather(input["distance"], 1, distance_index)  # [batch_size, n_loc]
        distance[mask] = 10000
        _, selected = distance.min(1)
        state = state.update(selected)
        sequences.append(selected)

    pi = torch.stack(sequences, 1)
    cost, _ = problem.get_costs(input, pi)

    if return_state:
        return cost, state
    else:
        return cost, state.serve_time


def check_insert(start, selected, tour, start_state, tmp_state_list):
    mask = start_state.get_mask().squeeze(1)  # [1, n_loc]
    if mask[0, selected] == 1:
        return False, None
    start_state = start_state.update(selected)
    tmp_state_list.append(copy.deepcopy(start_state))
    for s in range(start+1, len(tour)):
        selected = torch.LongTensor([tour[s]])
        mask = start_state.get_mask().squeeze(1)
        if mask[0, selected] == 1:
            return False, None
        start_state = start_state.update(selected)
        tmp_state_list.append(copy.deepcopy(start_state))

    return True, start_state


def single_insert(i, input, problem, opt):
    distance = input["distance"][0].view(1, -1)  # [1, 92*92]
    batch_size, n_loc = input['loc'].size(0), input['loc'].size(1) + 1
    single_input = {'loc': input['loc'][i:i + 1], 'demand': input['demand'][i:i + 1],
                    'distance': input['distance'][i:i + 1],
                    'duration': input['duration'][i:i + 1],
                    'tw_right': input['tw_right'][i:i + 1], 'tw_left': input['tw_left'][i:i + 1],
                    'fleet': input['fleet'][i:i + 1]}
    state = problem.make_state(single_input)
    state_list = [copy.deepcopy(state)]
    while not state.all_finished():
        mask = state.get_mask().squeeze(1)  # [1, n_loc]
        steps = state.tour.size(1)
        distance_index = state.NODE_SIZE * state.coords[0, state.tour].permute(1, 0).repeat(1, n_loc) + state.coords.repeat(steps, 1)  # [steps, n_loc]
        d = torch.gather(distance.repeat(steps, 1), 1, distance_index)  # [steps, n_loc]
        mask_ = mask.repeat(steps, 1)
        # selected node based on mask (can insert to the end anyway)
        if opt.val_method == "nearest_insert":
            d[:, 0] = 9999  # depot penalty
            d[mask_] = 10000
            selected = d.argmin() - torch.div(d.argmin(), n_loc, rounding_mode="floor") * n_loc
        elif opt.val_method == "farthest_insert":
            d[:, 0] = 1  # depot penalty
            d[mask_] = -10000
            selected = d.argmax() - torch.div(d.argmax(), n_loc, rounding_mode="floor") * n_loc
        elif opt.val_method == "random_insert":
            ids = torch.arange(n_loc)
            selected = ids[(mask == 0).view(-1)]
            random_id = random.randint(0, selected.size(0) - 1)
            selected = selected[random_id]
        selected = selected.view(-1)  # [1]
        # insert selected node to the proper position
        if state.prev_a.view(-1) == 0 or selected.item() == 0:  # add to the end
            state = state.update(selected)
            state_list.append(copy.deepcopy(state))
        else:
            dd, tour = {}, state.tour[0].tolist()
            for j in range(len(tour) - 1):  # 0->1, 1->2, ..., j-2->j-1
                old1, old2, new = state.coords[0, tour[j]].item(), state.coords[0, tour[j + 1]].item(), state.coords[0, selected.item()].item()
                dd[j] = distance[0][state.NODE_SIZE * old1 + new] + distance[0][state.NODE_SIZE * new + old2] - distance[0][state.NODE_SIZE * old1 + old2]
            sorted_dd = sorted(dd.items(), key=lambda item: item[1])
            for j in range(len(sorted_dd)):
                # check whether can insert between sorted_dd[j][0] and sorted_dd[j][0] + 1 or not
                start = sorted_dd[j][0]
                tmp_state_list = copy.deepcopy(state_list[:start + 1])
                start_state = tmp_state_list[start]
                insert, tmp_state = check_insert(start, selected, tour, start_state, tmp_state_list)
                if insert:
                    # print("insert to {} successfully".format(j))
                    state, state_list = tmp_state, tmp_state_list
                    break
                elif j == len(sorted_dd) - 1:  # fail to insert, add to the end instead.
                    state = state.update(selected)
                    state_list.append(copy.deepcopy(state))
    selected = torch.LongTensor([0])  # return to depot
    state = state.update(selected)

    return state, i


def insertion(input, problem, opt):
    res_list = []
    batch_size, n_loc = input['loc'].size(0), input['loc'].size(1) + 1
    cost, serve_time = torch.zeros(batch_size), torch.zeros(batch_size, n_loc)
    # if multiprocessing error, use command: ulimit -n 10240
    if opt.multiprocess:
        print(">> Val using multiprocessing")
        pool = Pool(processes=50)
        for i in range(batch_size):
            res = pool.apply_async(single_insert, args=(i, input, problem, opt))
            res_list.append(res)
        pool.close()
        pool.join()
        for r in res_list:
            state, i = r.get()
            cost[i], serve_time[i] = state.lengths.view(-1), state.serve_time.view(-1)
    else:
        for i in range(batch_size):
            state, _ = single_insert(i, input, problem, opt)
            # print(state.tour)
            cost[i], serve_time[i] = state.lengths.view(-1), state.serve_time.view(-1)

    return cost, serve_time


def stochastic_2_swap(input, problem, cur_tour):
    """
        For simulated_annealing to find a neighborhood of current solution,
        may not be feasible solution after swapping.
    """
    state = problem.make_state(input)
    feasibility = torch.zeros(cur_tour.size(0), dtype=torch.uint8, device=input["loc"].device)  # [batch_size]
    ids = torch.arange(cur_tour.size(0), device=input["loc"].device)
    left, right = random.randint(1, cur_tour.size(-1)-1), random.randint(1, cur_tour.size(-1)-1)
    if left > right:
        left, right = right, left
    start = 1
    while start < cur_tour.size(-1):
        if start == left:  # [left, right]
            selected = cur_tour[:, right]  # [batch_size]
        elif start == right:
            selected = cur_tour[:, left]
        else:
            selected = cur_tour[:, start]
        mask = state.get_mask()
        mask = mask[:, 0, :]  # [batch_size, n_loc+1]
        feasibility = feasibility | mask[ids, selected]
        state = state.update(selected)
        start += 1
    selected = torch.LongTensor([0] * cur_tour.size(0)).to(input["loc"].device)  # return to depot
    state = state.update(selected)
    return state, feasibility


def pad_tour(tour1, tour2):
    while True:
        if (tour1[-1] == 0).sum() == tour1.size(0):
            tour1 = tour1[:, :-1]
        elif (tour2[-1] == 0).sum() == tour2.size(0):
            tour2 = tour2[:, :-1]
        else:
            break
    if tour1.size(-1) > tour2.size(-1):
        tour2 = F.pad(tour2, (0, tour1.size(-1)-tour2.size(-1)), "constant", 0)
    elif tour1.size(-1) < tour2.size(-1):
        tour1 = F.pad(tour1, (0, tour2.size(-1) - tour1.size(-1)), "constant", 0)
    return tour1, tour2


def simulated_annealing(input, problem):
    count, start_t = 0, time.time()
    time_limit = 180  # for one fleet, total 1800
    neighbourhood_size, iterations, T = 500, 100, 200
    cost, state = nearest_neighbor(input, problem, return_state=True)
    print(">> init sol: {}".format(cost.mean()))
    serve_time, tour = state.serve_time, state.tour  # [batch_size, -1]
    cur_sol_cost, cur_sol_serve_time, cur_sol_tour = cost.detach().clone(), serve_time.detach().clone(), tour.detach().clone()
    best_sol_cost, best_sol_serve_time, best_sol_tour = cost.detach().clone(), serve_time.detach().clone(), tour.detach().clone()
    while (count < iterations):
        if time.time() - start_t > time_limit:
            break
        for i in range(0, neighbourhood_size):
            # preprocess cur_sol_tour
            while True:
                if (cur_sol_tour[:, -1] == 0).sum() == cur_sol_tour.size(0):
                    cur_sol_tour = cur_sol_tour[:, :-1]
                else:
                    break
            state, feasibility = stochastic_2_swap(input, problem, cur_sol_tour)
            new_sol_cost, new_sol_serve_time, new_sol_tour = state.lengths.view(-1), state.serve_time, state.tour
            delta_cost = new_sol_cost - cur_sol_cost
            # print(feasibility)
            ran_accept = np.random.uniform(0, 1, feasibility.size(0))
            criteria = np.e ** (-delta_cost / T)
            accept_id = (feasibility == 0) & ((delta_cost < 0) | (torch.Tensor(ran_accept).to(input["loc"].device) <= criteria))
            best_id = accept_id & (new_sol_cost < best_sol_cost)
            # print(accept_id, best_id)
            cur_sol_tour, new_sol_tour = pad_tour(cur_sol_tour, new_sol_tour)
            cur_sol_cost[accept_id], cur_sol_serve_time[accept_id], cur_sol_tour[accept_id] = new_sol_cost[accept_id], new_sol_serve_time[accept_id], new_sol_tour[accept_id]
            best_sol_tour, new_sol_tour = pad_tour(best_sol_tour, new_sol_tour)
            best_sol_cost[best_id], best_sol_serve_time[best_id], best_sol_tour[best_id] = new_sol_cost[best_id], new_sol_serve_time[best_id], new_sol_tour[best_id]
        count = count + 1
        T = T * 0.9

    print(">> After SA sol: {}".format(best_sol_cost.mean()))

    return best_sol_cost, best_sol_serve_time


def val(dataset, opt, fleet_info, distance, problem):
    cost = []
    for bat in tqdm(DataLoader(dataset, batch_size=50, shuffle=False), disable=opt.no_progress_bar):
        bat_cost = []
        bat_tw_left = bat['arrival'].repeat(len(fleet_info['next_duration']) + 1, 1, 1).to(opts.device)  # [6, batch_size, graph_size]
        bat_tw_right = bat['departure']  # [batch_size, graph_size]
        for f in fleet_info['order']:
            # merge more data
            next_duration = torch.tensor(fleet_info['next_duration'][fleet_info['precedence'][f]],
                                         device=bat['type'].device).repeat(bat['loc'].size(0), 1)  # [batch_size, 3]
            tw_right = bat_tw_right - torch.gather(next_duration, 1, bat['type'])
            tw_right = torch.cat((torch.full_like(tw_right[:, :1], 1441), tw_right), dim=1)  # [batch_size, graph_size+1]
            tw_left = bat_tw_left[fleet_info['precedence'][f]]
            tw_left = torch.cat((torch.zeros_like(tw_left[:, :1]), tw_left), dim=1)  # [batch_size, graph_size+1]
            duration = torch.tensor(fleet_info['duration'][f], device=bat['type'].device).repeat(bat['loc'].size(0), 1)  # [batch_size, 3]
            fleet_bat = {'loc': bat['loc'], 'demand': bat['demand'][:, f - 1, :],
                         'distance': distance.expand(bat['loc'].size(0), len(distance)),
                         'duration': torch.gather(duration, 1, bat['type']),
                         'tw_right': tw_right, 'tw_left': tw_left,
                         'fleet': torch.full((bat['loc'].size(0), 1), f - 1)}
            if opt.val_method == "cws":
                fleet_cost, serve_time = cws(move_to(fleet_bat, opt.device), problem, opt)
            elif opt.val_method == "nearest_neighbor":
                fleet_cost, serve_time = nearest_neighbor(move_to(fleet_bat, opt.device), problem)
            elif opt.val_method in ["nearest_insert", "farthest_insert", "random_insert"]:
                fleet_cost, serve_time = insertion(move_to(fleet_bat, opt.device), problem, opt)
            elif opt.val_method == "sa":
                fleet_cost, serve_time = simulated_annealing(move_to(fleet_bat, opt.device), problem)
            else:
                print(">> Unsupported val method!")
                return 0

            bat_cost.append(fleet_cost.data.cpu().view(-1, 1))
            # update tw_left
            bat_tw_left[fleet_info['precedence'][f] + 1] = torch.max(
                bat_tw_left[fleet_info['precedence'][f] + 1], serve_time[:, 1:])

        bat_cost = torch.cat(bat_cost, 1)
        cost.append(bat_cost)  # [batch_size, 10]

    cost = torch.cat(cost, 0)  # [dataset, 10]
    cost = cost.sum(1)
    print(cost.tolist())
    avg_cost = cost.mean()
    print('Validation overall avg_cost: {} +- {}'.format(avg_cost, torch.std(cost) / math.sqrt(len(cost))))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--filename", type=str, default="./data/agh/agh20_validation_seed4321.pkl", help="Filename of the dataset to load")
    parser.add_argument("--problem", type=str, default='agh', help="only support airport ground handling in this code")
    parser.add_argument('--graph_size', type=int, default=20, help="Sizes of problem instances (20, 50, 100)")
    parser.add_argument('--val_method', type=str, default='cws', choices=['cws', 'nearest_insert', 'farthest_insert',
                                                                          'random_insert', 'nearest_neighbor', 'sa'])
    parser.add_argument('--val_size', type=int, default=1000, help='Number of instances used for reporting validation performance')
    parser.add_argument('--offset', type=int, default=0, help='Offset where to start in dataset (default 0)')
    parser.add_argument('--seed', type=int, default=1234, help='Random seed to use')
    parser.add_argument('--no_cuda', action='store_true', help='Disable CUDA')
    parser.add_argument('--multiprocess', action='store_true', help='Using multiprocessing module')
    parser.add_argument('--no_progress_bar', action='store_true', help='Disable progress bar')

    opts = parser.parse_args()

    opts.use_cuda = torch.cuda.is_available() and not opts.no_cuda and \
                    opts.val_method not in ["nearest_insert", "farthest_insert", "random_insert"]
    opts.device = torch.device("cuda" if opts.use_cuda else "cpu")
    print(opts.device)

    pp.pprint(vars(opts))

    # Set the random seed
    random.seed(opts.seed)
    np.random.seed(opts.seed)
    torch.manual_seed(opts.seed)
    torch.cuda.manual_seed_all(opts.seed)

    # Figure out what's the problem
    problem_ = load_problem(opts.problem)

    val_dataset = problem_.make_dataset(filename=opts.filename, num_samples=opts.val_size, offset=opts.offset)

    with open('problems/agh/fleet_info.pkl', 'rb') as file_:
        fleet_info_ = pickle.load(file_)
    with open('problems/agh/distance.pkl', 'rb') as file_:
        distance_dict = pickle.load(file_)
    distance_ = torch.tensor(list(distance_dict.values()))

    print('Validating dataset: {}'.format(opts.filename))
    start_time = time.time()
    val(val_dataset, opts, fleet_info_, distance_, problem_)
    print(">> End of validation within {:.2f}s".format(time.time()-start_time))
